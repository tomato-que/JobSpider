@echo off
chcp 65001 >nul 2>nul
setlocal EnableExtensions
title JobSpider Launcher

rem ============================================================
rem  JobSpider launcher
rem
rem  Optional environment variables:
rem    JOBSPIDER_PYTHON  path to python.exe
rem    JOBSPIDER_HOST    bind address, default 127.0.0.1
rem    JOBSPIDER_PORT    bind port, default 8765
rem
rem  Usage:
rem    run.bat           start and open browser
rem    run.bat noopen    start only, do not open browser
rem ============================================================

set "APP_VERSION=2.2.0"
set "ROOT=%~dp0"
cd /d "%ROOT%"

if not exist "server.py" (
    echo [ERROR] server.py not found in:
    echo         %CD%
    echo         Put run.bat in the JobSpider project root next to server.py.
    pause
    exit /b 1
)

if not defined JOBSPIDER_HOST set "JOBSPIDER_HOST=127.0.0.1"
if not defined JOBSPIDER_PORT set "JOBSPIDER_PORT=8765"

rem 0.0.0.0 is a bind address only; the browser still uses loopback.
set "OPEN_HOST=%JOBSPIDER_HOST%"
if "%OPEN_HOST%"=="0.0.0.0" set "OPEN_HOST=127.0.0.1"

set "NO_OPEN="
if /I "%~1"=="noopen" set "NO_OPEN=1"

echo ========================================
echo   JobSpider v%APP_VERSION%
echo ========================================
echo.

call :find_python
if errorlevel 1 goto fail

echo [INFO] Python: %PY%
"%PY%" --version

"%PY%" -c "import sys; raise SystemExit(0 if sys.version_info >= (3,9) else 1)" >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Python 3.9+ is required.
    goto fail
)

call :check_deps
if errorlevel 1 goto fail

call :check_port
if errorlevel 1 goto fail

set "CHROME="
for /f "tokens=2,*" %%A in ('reg query "HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe" /ve 2^>nul ^| findstr /I "REG_SZ"') do set "CHROME=%%B"
if not defined CHROME for /f "tokens=2,*" %%A in ('reg query "HKCU\SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe" /ve 2^>nul ^| findstr /I "REG_SZ"') do set "CHROME=%%B"
if not defined CHROME if exist "%ProgramFiles%\Google\Chrome\Application\chrome.exe" set "CHROME=%ProgramFiles%\Google\Chrome\Application\chrome.exe"
if not defined CHROME if exist "%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe" set "CHROME=%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"
if not defined CHROME if exist "%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe" set "CHROME=%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"

echo.
echo [INFO] Starting server on http://%OPEN_HOST%:%JOBSPIDER_PORT%/
echo [INFO] Press Ctrl+C to stop; logs are shown below.
echo.

if not defined NO_OPEN (
    if defined CHROME (
        echo [INFO] Opening UI in Google Chrome.
        start "" /B powershell -NoProfile -WindowStyle Hidden -Command "for($i=0;$i -lt 40;$i++){try{Invoke-WebRequest -UseBasicParsing 'http://%OPEN_HOST%:%JOBSPIDER_PORT%/' -TimeoutSec 1 | Out-Null; Start-Process -FilePath $env:CHROME -ArgumentList 'http://%OPEN_HOST%:%JOBSPIDER_PORT%/'; break}catch{Start-Sleep -Milliseconds 700}}"
    ) else (
        echo [WARN] Google Chrome not found; opening the default browser instead.
        start "" /B powershell -NoProfile -WindowStyle Hidden -Command "for($i=0;$i -lt 40;$i++){try{Invoke-WebRequest -UseBasicParsing 'http://%OPEN_HOST%:%JOBSPIDER_PORT%/' -TimeoutSec 1 | Out-Null; Start-Process 'http://%OPEN_HOST%:%JOBSPIDER_PORT%/'; break}catch{Start-Sleep -Milliseconds 700}}"
    )
)

"%PY%" server.py
set "EXIT_CODE=%ERRORLEVEL%"

echo.
echo [INFO] Server stopped (exit code %EXIT_CODE%).
pause >nul
exit /b %EXIT_CODE%


rem ============================================================
rem  subroutines
rem ============================================================

:find_python
set "PY="
if defined JOBSPIDER_PYTHON if exist "%JOBSPIDER_PYTHON%" set "PY=%JOBSPIDER_PYTHON%"
if not defined PY if exist "%ROOT%.venv\Scripts\python.exe" set "PY=%ROOT%.venv\Scripts\python.exe"
for %%V in (3.9 3.10 3.11 3.12 3.13 3) do (
    if not defined PY for /f "usebackq delims=" %%P in (`py -%%V -c "import sys;print(sys.executable)" 2^>nul`) do set "PY=%%P"
)
for %%V in (39 310 311 312 313) do (
    if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python%%V\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python%%V\python.exe"
    if not defined PY if exist "%ProgramFiles%\Python%%V\python.exe" set "PY=%ProgramFiles%\Python%%V\python.exe"
    if not defined PY if exist "C:\Python%%V\python.exe" set "PY=C:\Python%%V\python.exe"
)
if not defined PY (
    where python >nul 2>nul
    if not errorlevel 1 set "PY=python"
)
if not defined PY (
    echo [ERROR] Python not found. Install Python 3.9+ or set JOBSPIDER_PYTHON.
    exit /b 1
)
exit /b 0


:check_deps
"%PY%" -c "import fastapi,uvicorn,pydantic,httpx,bs4,lxml,feedparser,dotenv,aiofiles,rich,dateutil,pypdf,docx,multipart" >nul 2>nul
if errorlevel 1 (
    echo [INFO] Installing dependencies from requirements.txt ...
    "%PY%" -m pip install --timeout 60 --retries 5 -r requirements.txt
    if errorlevel 1 (
        echo [ERROR] pip install failed.
        exit /b 1
    )
)

"%PY%" -c "import rebrowser_playwright" >nul 2>nul
if errorlevel 1 (
    "%PY%" -c "import playwright" >nul 2>nul
    if errorlevel 1 (
        echo [INFO] Installing Playwright engine ...
        "%PY%" -m pip install --timeout 60 --retries 5 -r requirements.txt
        if errorlevel 1 exit /b 1
    )
)
exit /b 0


:check_port
set "PORT_PID="
for /f "tokens=5" %%a in ('netstat -ano ^| findstr /R /C:":%JOBSPIDER_PORT% .*LISTENING"') do set "PORT_PID=%%a"
if not defined PORT_PID exit /b 0

set "PORT_CMD="
for /f "usebackq delims=" %%c in (`powershell -NoProfile -Command "(Get-CimInstance Win32_Process -Filter 'ProcessId=%PORT_PID%').CommandLine" 2^>nul`) do set "PORT_CMD=%%c"

if defined PORT_CMD (
    echo [WARN] Port %JOBSPIDER_PORT% is in use by PID %PORT_PID%:
    echo        %PORT_CMD%
) else (
    echo [WARN] Port %JOBSPIDER_PORT% is in use by PID %PORT_PID%.
)

echo %PORT_CMD% | findstr /I /C:"server.py" /C:"server:app" >nul
if errorlevel 1 (
    echo [ERROR] That is not a JobSpider server. Close it or set JOBSPIDER_PORT.
    exit /b 1
)

echo [INFO] Stopping the previous JobSpider server ...
taskkill /F /PID %PORT_PID% >nul 2>nul
ping -n 2 127.0.0.1 >nul

set "PORT_PID="
for /f "tokens=5" %%a in ('netstat -ano ^| findstr /R /C:":%JOBSPIDER_PORT% .*LISTENING"') do set "PORT_PID=%%a"
if defined PORT_PID (
    echo [ERROR] Port %JOBSPIDER_PORT% is still in use. Close the process and retry.
    exit /b 1
)
exit /b 0


:fail
echo.
pause
exit /b 1
