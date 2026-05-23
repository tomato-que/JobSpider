@echo off
chcp 65001 >nul 2>nul
title JobSpider Server

echo ========================================
echo   JobSpider v2.0 - Multi-Agent System
echo ========================================
echo.

cd /d "%~dp0"

:: Kill old process
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8765"') do (
    taskkill /F /PID %%a >nul 2>nul
)

:: Check Python
python --version >nul 2>nul
if %errorlevel% neq 0 (
    echo [ERROR] Python not found in PATH
    pause
    exit /b 1
)

:: Install deps if needed
python -c "import fastapi" >nul 2>nul
if %errorlevel% neq 0 (
    echo [INFO] Installing dependencies...
    pip install fastapi uvicorn pydantic playwright httpx -q
)

:: Start server in background
echo Starting server on http://localhost:8765 ...
start "" /B python -m uvicorn server:app --host 0.0.0.0 --port 8765

:: Poll until ready
echo Waiting for server to be ready...
set RETRY=0
:waitloop
timeout /t 2 /nobreak >nul
set /a RETRY+=1

curl -s http://localhost:8765/ >nul 2>nul
if %errorlevel% equ 0 goto ready

if %RETRY% lss 10 goto waitloop

echo [WARN] Server did not start after 20 seconds
echo [WARN] Check if dependencies are installed:
echo        pip install fastapi uvicorn pydantic playwright httpx
pause
exit /b 1

:ready
echo Server is ready (started in %RETRY% attempts)
echo.
echo ========================================
echo   Open: http://localhost:8765
echo   Press Ctrl+C to stop server
echo ========================================
start http://localhost:8765

echo.
echo Server is running... Close this window to stop.
pause >nul