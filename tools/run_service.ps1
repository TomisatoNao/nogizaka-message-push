# ============================================================
# run_service.ps1 — 守护循环：跑主程序，异常退出就自动拉起
# ============================================================
# ⚠️ 本文件必须保存为 【UTF-8 with BOM】（PowerShell 5.1 编码要求）
#
# 由 install_autostart.ps1 注册的计划任务调用，一般不需要手动执行。
# 手动跑（前台观察）：powershell -ExecutionPolicy Bypass -File tools\run_service.ps1
#
# 重启语义:
#   退出码 0   —— 主动停止（Ctrl+C / 优雅退出），守护循环结束，不再拉起
#   非 0       —— 崩溃 / 被杀，等待 RetrySeconds 后重新启动
#   网页端「重启主程序」用 os.execv 原地替换进程，PID 不变，
#   守护循环无感知，不会重复拉起
#
# 不依赖 Task Scheduler 自己的"失败后重启"策略 —— 那个策略只在任务整体
# 返回失败时才触发，子进程被杀而包装器正常退出的情况它捕捉不到。
# ============================================================
param(
    [int]$RetrySeconds = 60,
    [int]$MaxRestarts = 0      # 0 = 不限次数
)

$ErrorActionPreference = "Continue"
$RepoDir = Split-Path -Parent $PSScriptRoot
Set-Location $RepoDir

$python = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $python) {
    Write-Error "找不到 python"
    exit 1
}
# 优先使用仓库内的虚拟环境
$venv = Join-Path $RepoDir ".venv\Scripts\python.exe"
if (Test-Path $venv) { $python = $venv }

$logDir = Join-Path $RepoDir "logs"
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }
$logFile = Join-Path $logDir "service.log"

function Write-ServiceLog($message) {
    $line = "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $message
    Write-Host $line
    Add-Content -Path $logFile -Value $line -Encoding utf8
}

Write-ServiceLog "守护循环启动（python: $python）"
$restarts = 0

while ($true) {
    $started = Get-Date
    & $python main.py
    $code = $LASTEXITCODE
    $ranFor = [int]((Get-Date) - $started).TotalSeconds

    if ($code -eq 0) {
        Write-ServiceLog "主程序正常退出（运行 ${ranFor}s），守护循环结束"
        break
    }

    $restarts++
    if ($MaxRestarts -gt 0 -and $restarts -ge $MaxRestarts) {
        Write-ServiceLog "主程序异常退出（码 $code），已达重启上限 $MaxRestarts，放弃"
        exit 1
    }

    # 启动即崩溃（如配置错误）时拉长间隔，避免疯狂重启刷屏
    $wait = if ($ranFor -lt 30) { [Math]::Min($RetrySeconds * 5, 300) } else { $RetrySeconds }
    Write-ServiceLog "主程序异常退出（码 $code，运行 ${ranFor}s），${wait}s 后重启（第 $restarts 次）"
    Start-Sleep -Seconds $wait
}
