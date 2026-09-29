<#
.SYNOPSIS
    随行管家 · 一键启动（本地演示）

.DESCRIPTION
    依次做四件事，每件事都会先检查"是不是已经在跑了"：
      1. 拉起 PostgreSQL（它不会开机自启，每次都要先拉起来）
      2. 拉起后端 FastAPI（:5000）
      3. 拉起前端 Vite（:5173）
      4. 等后端就绪，打印入口地址并打开浏览器

.EXAMPLE
    .\启动本地.ps1                # 正常启动（最后打开浏览器）
    .\启动本地.ps1 -NoBrowser     # 不打开浏览器（自动化/自检时用）

.NOTE
    ⚠️ 本文件必须保存为「UTF-8 带 BOM」。
    PowerShell 5.1 默认按 GBK 读没有 BOM 的 UTF-8，中文会变乱码并导致语法错误——
    报错信息看起来像脚本写错了，实际只是编码问题。

    双击请用同目录的「启动本地.cmd」或「启动项目.lnk」，
    它们会用 -ExecutionPolicy Bypass 调用本脚本，绕开"禁止运行脚本"的限制。
#>

param(
    [switch]$NoBrowser
)

$ErrorActionPreference = 'Continue'

# Windows 下 Python 默认按 GBK 输出，遇到中文或特殊字符会抛 UnicodeEncodeError。
# 这个异常发生在"打印日志"这一步，却会中断真正的业务流程——所以强制 UTF-8。
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'

# ---------- 路径 ----------
$root       = Split-Path -Parent $PSScriptRoot          # hotel-agent
$backend    = Join-Path $root 'backend'
$web        = Join-Path $root 'web'
$logs       = Join-Path $root 'logs'
$parentDir  = Split-Path -Parent $root                  # 工作区根目录（.venv 在这里）
$venvPython = Join-Path $parentDir '.venv\Scripts\python.exe'
if (-not (Test-Path $venvPython)) { $venvPython = 'python' }

# PostgreSQL 由 anaconda 环境提供，数据目录在工作区内
$pgbin  = 'D:\anaconda\envs\pg\Library\bin'
$pgdata = Join-Path $parentDir 'pgdata'

$backendUrl  = 'http://127.0.0.1:5000'
$frontendUrl = 'http://localhost:5173'

New-Item -ItemType Directory -Force -Path $logs | Out-Null

function Test-Port([int]$Port) {
    $client = New-Object System.Net.Sockets.TcpClient
    try   { return $client.ConnectAsync('127.0.0.1', $Port).Wait(800) }
    catch { return $false }
    finally { $client.Close() }
}

function Test-Http([string]$Url) {
    try {
        $resp = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 3
        return ($resp.StatusCode -eq 200)
    } catch {
        return $false
    }
}

Write-Host ''
Write-Host '===== 随行管家 · 本地启动 =====' -ForegroundColor Cyan

# ---------- 0. 外网连通性 ----------
#
# 为什么放在最前面：知识检索与生成都要调百炼（DashScope）。
# 这条链路不通时，服务照样能起来、页面照样能打开，但你一问问题就会失败——
# **问题在使用时才暴露，而根因在启动前就已经存在**。提前几秒检查，
# 能把"用了才发现"变成"启动就知道"。
Write-Host ''
Write-Host '[0/4] 检查外网（向量化与生成都要调百炼）...' -ForegroundColor Yellow
$canReach = $false
try {
    $netClient = New-Object System.Net.Sockets.TcpClient
    $canReach = $netClient.ConnectAsync('dashscope.aliyuncs.com', 443).Wait(4000) -and $netClient.Connected
    $netClient.Close()
} catch { $canReach = $false }
if ($canReach) {
    Write-Host '      可以连到百炼' -ForegroundColor Green
} else {
    Write-Host '      ⚠️ 连不上 dashscope.aliyuncs.com:443' -ForegroundColor Red
    Write-Host '         服务仍会启动、页面也能打开，但：' -ForegroundColor Yellow
    Write-Host '           · 知识检索会降级为本地关键词检索（回答里会标注"检索降级提示"）' -ForegroundColor Yellow
    Write-Host '           · 生成会明确报"服务暂时不可用"，不会返回伪回答' -ForegroundColor Yellow
}

# ---------- 1. PostgreSQL ----------
Write-Host ''
Write-Host '[1/4] 检查 PostgreSQL ...' -ForegroundColor Yellow
if (-not (Test-Path "$pgbin\pg_ctl.exe")) {
    Write-Host '      找不到 pg_ctl.exe，跳过（业务库是硬依赖，起不来就会报错）' -ForegroundColor Red
} else {
    $isReady = (& "$pgbin\pg_isready.exe" -h 127.0.0.1 -p 5432 2>&1) -match 'accepting connections'
    if ($isReady) {
        Write-Host '      已经在运行，跳过' -ForegroundColor Green
    } else {
        Write-Host '      正在启动 ...'
        & "$pgbin\pg_ctl.exe" -D $pgdata -o '-p 5432' -l (Join-Path $pgdata 'server.log') start 2>&1 | Out-Null
        for ($i = 0; $i -lt 30; $i++) {
            Start-Sleep -Milliseconds 500
            if ((& "$pgbin\pg_isready.exe" -h 127.0.0.1 -p 5432 2>&1) -match 'accepting connections') {
                $isReady = $true; break
            }
        }
        if ($isReady) { Write-Host '      已启动' -ForegroundColor Green }
        else { Write-Host '      启动失败，看 pgdata\server.log' -ForegroundColor Red }
    }
}

# ---------- 2. 后端 ----------
Write-Host ''
Write-Host '[2/4] 启动后端服务 ...' -ForegroundColor Yellow
if (Test-Port 5000) {
    Write-Host '      端口 5000 已被占用，认为服务已在运行，跳过启动' -ForegroundColor Green
} else {
    Start-Process -FilePath $venvPython `
        -ArgumentList 'run.py' `
        -WorkingDirectory $backend `
        -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $logs 'backend.out.log') `
        -RedirectStandardError  (Join-Path $logs 'backend.err.log')

    $ok = $false
    foreach ($i in 1..40) {
        Start-Sleep -Milliseconds 750
        if (Test-Port 5000) { $ok = $true; break }
    }
    if ($ok) { Write-Host '      已启动' -ForegroundColor Green }
    else {
        Write-Host '      未在 30 秒内就绪，请看 logs\backend.err.log' -ForegroundColor Red
        Get-Content (Join-Path $logs 'backend.err.log') -Tail 15 -ErrorAction SilentlyContinue
    }
}

# ---------- 3. 前端 ----------
Write-Host ''
Write-Host '[3/4] 启动前端页面 ...' -ForegroundColor Yellow
if (-not (Test-Path (Join-Path $web 'node_modules'))) {
    Write-Host '      未安装依赖，跳过。需要时执行：cd web; npm install' -ForegroundColor Yellow
} elseif (Test-Http $frontendUrl) {
    # 注意：这里必须用 HTTP 探测，**不能用 TCP 探 127.0.0.1:5173**——
    # vite 绑定的是 localhost（本机解析到 IPv6 的 ::1），用 IPv4 探会得到假阴性，
    # 于是重复拉起一个前端（实测踩过：多出两个 node 进程）。
    Write-Host '      前端已在运行，跳过启动' -ForegroundColor Green
} else {
    Start-Process -FilePath 'npm.cmd' `
        -ArgumentList 'run', 'dev' `
        -WorkingDirectory $web `
        -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $logs 'web.out.log') `
        -RedirectStandardError  (Join-Path $logs 'web.err.log')
    for ($i = 0; $i -lt 30; $i++) {
        Start-Sleep -Milliseconds 500
        if (Test-Port 5173) { break }
    }
    Write-Host '      已启动（vite 绑定 localhost，用 localhost 访问）' -ForegroundColor Green
}

# ---------- 4. 等后端就绪 ----------
Write-Host ''
Write-Host '[4/4] 等待后端就绪 ...' -ForegroundColor Yellow
$healthy = $false
for ($i = 0; $i -lt 60; $i++) {
    if (Test-Http "$backendUrl/api/health") { $healthy = $true; break }
    Start-Sleep -Milliseconds 500
}

Write-Host ''
if ($healthy) {
    Write-Host '===== 启动完成 =====' -ForegroundColor Green
    Write-Host ''
    Write-Host "  住客端（演示入口）: $frontendUrl" -ForegroundColor Cyan
    Write-Host "  坐席台            : $frontendUrl  → 右上角切到「坐席台」" -ForegroundColor Cyan
    Write-Host "  接口文档          : $backendUrl/api/docs" -ForegroundColor Cyan
    Write-Host "  依赖自检          : $backendUrl/api/health?deep=1" -ForegroundColor Cyan
    Write-Host ''
    Write-Host '  试一句 : 能送两瓶矿泉水吗？（会先确认身份：房号 1208 / 姓氏 陈 / 离店日 2026-09-18）' -ForegroundColor DarkGray
    Write-Host '  停止   : 双击 工具\停止本地.cmd' -ForegroundColor DarkGray

    if (-not $NoBrowser) {
        Write-Host ''
        Write-Host '  正在打开浏览器 ...' -ForegroundColor Green
        Start-Process $frontendUrl
    }
} else {
    Write-Host '===== 服务没能就绪 =====' -ForegroundColor Red
    Write-Host ''
    Write-Host '  看一下错误日志：' -ForegroundColor Yellow
    Write-Host "    $(Join-Path $logs 'backend.err.log')" -ForegroundColor Yellow
    Write-Host "    $(Join-Path $logs 'web.err.log')" -ForegroundColor Yellow
    Get-Content (Join-Path $logs 'backend.err.log') -Tail 20 -ErrorAction SilentlyContinue
}
Write-Host ''
