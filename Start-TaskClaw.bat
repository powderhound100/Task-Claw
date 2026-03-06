@echo off
title Task-Claw Agent
echo ======================================
echo   Task-Claw Coding Agent
echo ======================================
echo.

:: Load env vars from .env if not already set
if "%GITHUB_TOKEN%"=="" (
    for /f "tokens=1,* delims==" %%A in ('findstr /B "GITHUB_TOKEN" "%~dp0.env" 2^>nul') do (
        set "%%A=%%B"
    )
)
if "%GITHUB_TOKEN%"=="" (
    echo ERROR: GITHUB_TOKEN is not set!
    echo.
    echo Set it in .env as: GITHUB_TOKEN=ghp_your_token_here
    echo.
    pause
    exit /b 1
)

:: Load other env vars from .env
for /f "usebackq tokens=1,* delims==" %%A in ("%~dp0.env") do (
    set "line=%%A"
    if not "!line:~0,1!"=="#" (
        if not defined %%A set "%%A=%%B"
    )
)

cd /d "%~dp0"
python task-claw.py
pause
