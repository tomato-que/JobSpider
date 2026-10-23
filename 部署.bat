@echo off
chcp 65001 >nul 2>nul
setlocal EnableExtensions
title JobSpider Deploy

rem ============================================================
rem  JobSpider deployment checker / installer
rem
rem  Checks and installs only what is missing:
rem    - Python 3.9+
rem    - pip
rem    - Python packages from requirements.txt
rem    - rebrowser-playwright / playwright driver
rem    - Google Chrome (required: Playwright channel="chrome")
rem    - data/ output/ chrome_profile/ and .env
rem
rem  Existing components are skipped.
rem
rem  Usage:
rem    double-click this file
rem    this file with "nostart" argument: check only, do not offer to start
rem
rem  Optional:
rem    JOBSPIDER_PYTHON     path to python.exe
rem ============================================================

set "APP_VERSION=2.3.1"
set "ROOT=%~dp0"
cd /d "%ROOT%"

set "NO_START="
if /I "%~1"=="nostart" set "NO_START=1"

echo ========================================
echo   JobSpider v%APP_VERSION% - Deploy
echo ========================================
echo.

if not exist "server.py" (
    echo [ERROR] server.py not found in:
    echo         %CD%
    echo         Put this script in the JobSpider project root next to server.py.
    echo         If you downloaded a ZIP, extract the whole archive first.
    goto fail
)
if not exist "requirements.txt" (
    echo [ERROR] requirements.txt not found in:
    echo         %CD%
    echo         Extract the complete JobSpider archive, then run this script again.
    goto fail
)

call :find_python
if not defined PY (
    echo [MISSING] Python 3.9+
    call :install_python
    if errorlevel 1 goto fail
    ping -n 4 127.0.0.1 >nul
    call :find_python
    if not defined PY (
        echo [ERROR] Python was installed but is not visible in this window.
        echo         Close and reopen this file, or set JOBSPIDER_PYTHON.
        goto fail
    )
) else (
    echo [SKIP] Python: %PY% %PY_ARGS%
)

"%PY%" %PY_ARGS% -c "import sys; raise SystemExit(0 if sys.version_info >= (3,9) else 1)" >nul 2>nul
if errorlevel 1 (
    echo [MISSING] Python 3.9+ - current version:
    "%PY%" %PY_ARGS% --version
    call :install_python
    if errorlevel 1 goto fail
    ping -n 4 127.0.0.1 >nul
    call :find_python
    if not defined PY (
        echo [ERROR] Python was installed but is not visible in this window.
        echo         Close and reopen this file, or set JOBSPIDER_PYTHON.
        goto fail
    )
    "%PY%" %PY_ARGS% -c "import sys; raise SystemExit(0 if sys.version_info >= (3,9) else 1)" >nul 2>nul
    if errorlevel 1 (
        echo [ERROR] Python 3.9+ is still not available.
        echo         Detected: %PY% %PY_ARGS%
        "%PY%" %PY_ARGS% --version
        goto fail
    )
)
"%PY%" %PY_ARGS% --version
echo.

call :ensure_pip
if errorlevel 1 goto fail

call :ensure_deps
if errorlevel 1 goto fail

call :ensure_engines
if errorlevel 1 goto fail

call :ensure_chrome
if errorlevel 1 goto fail

call :ensure_dirs_env
if errorlevel 1 goto fail

echo.
echo [CHECK] Importing server...
"%PY%" %PY_ARGS% -c "import server" >nul 2>nul
if errorlevel 1 (
    echo [ERROR] server import failed. Details:
    "%PY%" %PY_ARGS% -c "import server"
    goto fail
)
echo [OK] server import

echo.
echo ========================================
echo   Deployment check complete
echo ========================================
echo   Python : %PY% %PY_ARGS%
echo   Chrome : %CHROME%
echo   Data   : data\ output\ chrome_profile\
echo   Config : .env
echo ========================================
echo.

if defined NO_START goto done

choice /C YN /N /D N /T 15 /M "Start JobSpider now? [Y/N, default N in 15s] "
if errorlevel 2 goto done
call "%ROOT%run.bat"
goto done


:done
echo.
echo [INFO] Deployment finished. Double-click run.bat to start later.
pause
exit /b 0


:fail
echo.
echo [ERROR] Deployment did not complete. Fix the error above and retry.
pause
exit /b 1


rem ============================================================
rem  subroutines
rem ============================================================

:find_python
set "PY="
set "PY_ARGS="
if defined JOBSPIDER_PYTHON if exist "%JOBSPIDER_PYTHON%" set "PY=%JOBSPIDER_PYTHON%"
if not defined PY if exist "%ROOT%.venv\Scripts\python.exe" set "PY=%ROOT%.venv\Scripts\python.exe"

rem Ask the py launcher first: it knows custom install locations too.
for %%V in (3.9 3.10 3.11 3.12 3.13) do (
    if not defined PY (
        py -%%V -c "import sys" >nul 2>nul
        if not errorlevel 1 (
            set "PY=py"
            set "PY_ARGS=-%%V"
        )
    )
)
if not defined PY (
    py -3 -c "import sys" >nul 2>nul
    if not errorlevel 1 (
        set "PY=py"
        set "PY_ARGS=-3"
    )
)

rem Fall back to concrete install paths (python.org / winget).
for %%V in (39 310 311 312 313) do (
    if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python%%V\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python%%V\python.exe"
    if not defined PY if exist "%ProgramFiles%\Python%%V\python.exe" set "PY=%ProgramFiles%\Python%%V\python.exe"
    if not defined PY if exist "C:\Python%%V\python.exe" set "PY=C:\Python%%V\python.exe"
)
if not defined PY (
    where python >nul 2>nul
    if not errorlevel 1 set "PY=python"
)
exit /b 0


:install_python
where winget >nul 2>nul
if errorlevel 1 (
    echo [ERROR] winget is not available. Install Python 3.9+ manually:
    echo         https://www.python.org/downloads/
    start "" "https://www.python.org/downloads/"
    exit /b 1
)
choice /C YN /N /D Y /T 15 /M "Install Python 3.12 with winget? [Y/N, default Y in 15s] "
if errorlevel 2 exit /b 1
echo [INSTALL] Python 3.12 ...
winget install --id Python.Python.3.12 -e --source winget --accept-package-agreements --accept-source-agreements
exit /b %ERRORLEVEL%


:ensure_pip
"%PY%" %PY_ARGS% -m pip --version >nul 2>nul
if not errorlevel 1 (
    echo [SKIP] pip
    exit /b 0
)
echo [INSTALL] pip ...
"%PY%" %PY_ARGS% -m ensurepip --upgrade
if errorlevel 1 exit /b 1
"%PY%" %PY_ARGS% -m pip --version >nul 2>nul
exit /b %ERRORLEVEL%


:ensure_deps
"%PY%" %PY_ARGS% -c "import fastapi,uvicorn,pydantic,httpx,bs4,lxml,feedparser,dotenv,aiofiles,rich,dateutil,pypdf,docx,multipart" >nul 2>nul
if not errorlevel 1 (
    echo [SKIP] Python packages
    exit /b 0
)

echo [INSTALL] Python packages from requirements.txt ...
"%PY%" %PY_ARGS% -m pip install --timeout 60 --retries 5 -r requirements.txt
if errorlevel 1 (
    if defined VIRTUAL_ENV exit /b 1
    echo [RETRY] pip install --user ...
    "%PY%" %PY_ARGS% -m pip install --timeout 60 --retries 5 --user -r requirements.txt
    if errorlevel 1 exit /b 1
)

"%PY%" %PY_ARGS% -c "import fastapi,uvicorn,pydantic,httpx,bs4,lxml,feedparser,dotenv,aiofiles,rich,dateutil,pypdf,docx,multipart" >nul 2>nul
if errorlevel 1 exit /b 1
echo [OK] Python packages
exit /b 0


:ensure_engines
"%PY%" %PY_ARGS% -c "import rebrowser_playwright" >nul 2>nul
if not errorlevel 1 (
    echo [SKIP] rebrowser-playwright
    exit /b 0
)
"%PY%" %PY_ARGS% -c "import playwright" >nul 2>nul
if not errorlevel 1 (
    echo [SKIP] playwright
    exit /b 0
)
echo [INSTALL] Playwright engine ...
"%PY%" %PY_ARGS% -m pip install --timeout 60 --retries 5 -r requirements.txt
if errorlevel 1 exit /b 1
echo [OK] Playwright engine
exit /b 0


:ensure_chrome
call :find_chrome
if defined CHROME (
    echo [SKIP] Google Chrome: %CHROME%
    exit /b 0
)

echo [MISSING] Google Chrome
echo          JobSpider uses installed Chrome via Playwright channel="chrome".
echo          Playwright Chromium and Microsoft Edge are not used.
where winget >nul 2>nul
if errorlevel 1 (
    echo [ERROR] winget is not available. Install Chrome manually:
    start "" "https://www.google.com/chrome/"
    exit /b 1
)
choice /C YN /N /D Y /T 15 /M "Install Google Chrome with winget? [Y/N, default Y in 15s] "
if errorlevel 2 (
    start "" "https://www.google.com/chrome/"
    exit /b 1
)
echo [INSTALL] Google Chrome ...
winget install --id Google.Chrome -e --source winget --accept-package-agreements --accept-source-agreements
call :find_chrome
if not defined CHROME (
    echo [ERROR] Chrome still not found. Install manually:
    start "" "https://www.google.com/chrome/"
    exit /b 1
)
echo [OK] Google Chrome: %CHROME%
exit /b 0


:find_chrome
set "CHROME="
for /f "tokens=2,*" %%A in ('reg query "HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe" /ve 2^>nul ^| findstr /I "REG_SZ"') do set "CHROME=%%B"
if not defined CHROME for /f "tokens=2,*" %%A in ('reg query "HKCU\SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe" /ve 2^>nul ^| findstr /I "REG_SZ"') do set "CHROME=%%B"
if not defined CHROME if exist "%ProgramFiles%\Google\Chrome\Application\chrome.exe" set "CHROME=%ProgramFiles%\Google\Chrome\Application\chrome.exe"
if not defined CHROME if exist "%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe" set "CHROME=%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"
if not defined CHROME if exist "%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe" set "CHROME=%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"
exit /b 0


:ensure_dirs_env
for %%D in (data output chrome_profile) do (
    if not exist "%%D" (
        mkdir "%%D"
        echo [CREATE] %%D\
    )
)
if not exist ".env" (
    if exist ".env.template" (
        copy /Y ".env.template" ".env" >nul
        echo [CREATE] .env from .env.template
    ) else (
        echo [WARN] .env.template not found; create .env manually if LLM is needed.
    )
) else (
    echo [SKIP] .env
)
exit /b 0
