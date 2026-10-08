@echo off
REM ============================================================
REM  Bambu Fleet Manager - Windows launcher
REM
REM  Usage:
REM    run.bat                  serve on the configured host/port
REM    run.bat --reload         auto-reload on source changes
REM    run.bat --host 0.0.0.0 --port 8000
REM    run.bat --dev            use requirements-dev.txt
REM    run.bat --help
REM
REM  Host and port default to config/settings.json (editable from the
REM  dashboard's gear menu); command-line flags override them.
REM ============================================================
setlocal enabledelayedexpansion

cd /d "%~dp0"

set "HOST=0.0.0.0"
set "PORT=8000"
set "RELOAD="
set "DEV="
set "PORT_SET="
set "HOST_SET="

:parse
if "%~1"=="" goto setup
if /I "%~1"=="--reload" (
    set "RELOAD=--reload"
    shift & goto parse
)
if /I "%~1"=="--dev" (
    set "DEV=1"
    shift & goto parse
)
if /I "%~1"=="--host" (
    set "HOST=%~2"
    set "HOST_SET=1"
    shift & shift & goto parse
)
if /I "%~1"=="--port" (
    set "PORT=%~2"
    set "PORT_SET=1"
    shift & shift & goto parse
)
if /I "%~1"=="--help" goto help
if /I "%~1"=="-h" goto help
echo [bfm] Unknown option: %~1
goto help

:help
echo Usage: run.bat [--reload] [--host 0.0.0.0] [--port 8000] [--dev]
exit /b 0

:setup
if not exist "config" mkdir "config"
if not exist "uploads" mkdir "uploads"

if exist ".venv\Scripts\python.exe" goto deps
echo [bfm] Creating virtual environment in .venv ...
where py >nul 2>&1 && (py -3 -m venv .venv) || (python -m venv .venv)
if not exist ".venv\Scripts\python.exe" (
    echo [bfm] ERROR: could not create the virtual environment. Install Python 3.10+ first.
    exit /b 1
)

:deps
set "PYTHONUTF8=1"
set "PYTHONUNBUFFERED=1"
set "PY=.venv\Scripts\python.exe"

set "REQ=requirements.txt"
if defined DEV set "REQ=requirements-dev.txt"
echo [bfm] Installing Python dependencies from %REQ% ...
"%PY%" -m pip install --disable-pip-version-check --quiet -r "%REQ%"
if errorlevel 1 (
    echo [bfm] ERROR: dependency installation failed.
    exit /b 1
)

REM --- apply saved settings unless the command line overrode them ---
REM (read via tools/get_setting.py: inline -c with quotes breaks cmd parsing)
if not defined PORT_SET (
    for /f "delims=" %%P in ('%PY% tools\get_setting.py port') do set "PORT=%%P"
)
if not defined HOST_SET (
    for /f "delims=" %%H in ('%PY% tools\get_setting.py host') do set "HOST=%%H"
)

:run
set "VER="
for /f "usebackq delims=" %%V in ("VERSION") do set "VER=%%V"

echo.
echo   Bambu Fleet Manager v!VER!
echo   Dashboard : http://localhost:%PORT%/
echo   Listening: %HOST%:%PORT%
echo   Transport : MQTTS 8883 / FTPS 990 / RTSPS 322 - LAN only
echo   Stopping  : press Ctrl+C
echo.
"%PY%" -m uvicorn backend.main:app --host %HOST% --port %PORT% %RELOAD%
set "EXITCODE=%ERRORLEVEL%"
echo [bfm] Server exited with code %EXITCODE%.
endlocal & exit /b %EXITCODE%
