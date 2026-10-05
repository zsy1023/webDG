@echo off
rem ==============================================================
rem  webDG one-click launcher  (double-click this file)
rem
rem  First run: creates .venv here, installs deps (via local proxy
rem  7897 when available), then starts all nodes and opens the panel.
rem  Later runs: fast (environment already ready).
rem
rem  All user-facing text (Chinese) lives in start.ps1 -- this .cmd is
rem  deliberately ASCII-only: cmd.exe decodes .cmd as the OEM code page
rem  and would choke on UTF-8 comments containing box-drawing chars
rem  (measured: it tried to execute the comment text as commands).
rem
rem  Options (passed straight through to start.ps1):
rem    -Setup          install + self-check only, do not start nodes
rem    -Reinstall      rebuild .venv from scratch
rem    -Role b         cross-machine peer: set [bridge] role = "b"
rem    -Port 8888      panel port
rem    -NoRelay        do not start the local V4 relay
rem    -Proxy http://...   override pip proxy
rem    -NoPause        no "press enter" at the end (for scripts)
rem ==============================================================
setlocal
cd /d "%~dp0"
chcp 65001 >nul 2>&1
title webDG

where powershell >nul 2>&1
if errorlevel 1 (
    echo.
    echo   [x] PowerShell not found ^(Windows built-in; maybe blocked by policy^)
    echo.
    pause
    exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" %*
if errorlevel 1 (
    echo.
    echo   [x] Start failed - see the reason above.
    pause >nul
)
endlocal
