# 随行管家 · 停止本地服务
# 只结束监听 :5000（后端）与 :5173（前端）的进程，不动数据库。

$ErrorActionPreference = 'SilentlyContinue'

foreach ($port in 5000, 5173) {
    $conns = Get-NetTCPConnection -LocalPort $port -State Listen
    if (-not $conns) {
        Write-Host "端口 $port 没有在监听" -ForegroundColor Yellow
        continue
    }
    foreach ($pid in ($conns.OwningProcess | Select-Object -Unique)) {
        $proc = Get-Process -Id $pid
        Write-Host "结束 $($proc.ProcessName) (PID $pid) —— 端口 $port" -ForegroundColor Cyan
        Stop-Process -Id $pid -Force
    }
}
Write-Host '已停止（PostgreSQL 未受影响）' -ForegroundColor Green
