@echo off
rem ==============================================================
rem  Stop webDG -- kills only the processes of THIS release folder
rem  (matched by the current directory appearing in their command
rem  line, so unrelated python processes are never touched).
rem
rem  ASCII-only on purpose: cmd.exe decodes .cmd as the OEM code page
rem  and would execute UTF-8 Chinese comments as commands (measured).
rem  The Chinese messages live in stop.ps1.
rem ==============================================================
setlocal
cd /d "%~dp0"
chcp 65001 >nul 2>&1
title webDG stop

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0stop.ps1" %*
if errorlevel 1 (
    echo.
    echo   [x] Stop script failed - see the reason above.
    pause >nul
)
endlocal
