@echo off
rem One-click stop. ASCII-only, see start-local.cmd for why.
cd /d "%~dp0"
powershell -ExecutionPolicy Bypass -File "%~dp0stop-local.ps1"
pause
