@echo off
rem One-click start. Keep this file ASCII-only: cmd.exe parses .cmd using the
rem console code page, so non-ASCII here breaks on UTF-8 vs GBK consoles.
cd /d "%~dp0"
powershell -ExecutionPolicy Bypass -File "%~dp0start-local.ps1"
pause
