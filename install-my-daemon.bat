@echo off
REM My Daemon - one-file Windows installer.
REM
REM Download this file and double-click it. It finds Python, fetches the
REM installer, and hands over. Everything else happens in scripts/bootstrap.py,
REM which lives in the repo where it can be read and tested.
REM
REM Spec: docs/superpowers/specs/2026-07-28-windows-bootstrap-installer-design.md

setlocal enabledelayedexpansion

set "BOOTSTRAP_URL=https://raw.githubusercontent.com/evangress/my-daemon/master/scripts/bootstrap.py"
set "BOOTSTRAP_FILE=%TEMP%\my-daemon-bootstrap.py"

echo === My Daemon - installer ===
echo.

REM ----------------------------------------------------------------- Python
REM The py launcher first: it is the one entry point that survives a user
REM having several Pythons installed.
set "PY="
where py >nul 2>&1 && set "PY=py -3"
if not defined PY where python >nul 2>&1 && set "PY=python"

if not defined PY (
    echo Python was not found on this machine.
    echo.
    where winget >nul 2>&1
    if errorlevel 1 (
        echo Install Python 3.12 from https://www.python.org/downloads/
        echo Tick "Add python.exe to PATH" during setup, then run this file again.
        echo.
        pause
        exit /b 1
    )
    set /p "ANSWER=Install Python 3.12 now? [y/N] "
    if /i not "!ANSWER!"=="y" (
        echo Cancelled - Python is required to run My Daemon.
        pause
        exit /b 1
    )
    echo.
    echo Installing Python 3.12 via winget...
    winget install --id Python.Python.3.12 --exact --source winget --accept-package-agreements --accept-source-agreements
    echo.

    REM winget updates PATH for *new* processes, not this one, so a re-check
    REM here usually still fails. Say so plainly instead of looking broken.
    set "PY="
    where py >nul 2>&1 && set "PY=py -3"
    if not defined PY (
        echo Python is installed, but this window cannot see it yet.
        echo Close this window and double-click this file again to finish.
        echo.
        pause
        exit /b 0
    )
)

echo Using Python: !PY!
echo.

REM ------------------------------------------------------------- bootstrap
REM Printed rather than fetched silently: this file downloads and runs code,
REM and you should be able to see exactly what it is about to run.
echo Fetching the installer from:
echo   %BOOTSTRAP_URL%
echo.

curl.exe -fsSL "%BOOTSTRAP_URL%" -o "%BOOTSTRAP_FILE%" 2>nul
if errorlevel 1 (
    REM curl.exe ships with Windows 10 1803+; PowerShell covers anything older.
    powershell -NoProfile -ExecutionPolicy Bypass -Command "try { Invoke-WebRequest -UseBasicParsing -Uri '%BOOTSTRAP_URL%' -OutFile '%BOOTSTRAP_FILE%' } catch { exit 1 }"
)

if not exist "%BOOTSTRAP_FILE%" (
    echo [ERROR] Could not download the installer.
    echo Check your internet connection and that github.com is reachable.
    echo.
    pause
    exit /b 1
)

!PY! "%BOOTSTRAP_FILE%"
set "RESULT=!ERRORLEVEL!"

del "%BOOTSTRAP_FILE%" >nul 2>&1

echo.
if not "!RESULT!"=="0" echo Installation did not complete. Scroll up for the reason.
pause
exit /b !RESULT!
