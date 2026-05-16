@echo off
REM My Daemon — Windows one-click bootstrap.
REM Double-click this file (or run from cmd) to set up the project.

setlocal
cd /d "%~dp0"

echo === My Daemon - Windows setup ===
echo Working directory: %CD%
echo.

REM Prefer the Python launcher (py.exe) if available; fall back to python on PATH.
where py >nul 2>&1
if %ERRORLEVEL%==0 (
    set "PY=py -3"
) else (
    where python >nul 2>&1
    if %ERRORLEVEL% NEQ 0 (
        echo [ERROR] Python is not on PATH and the py launcher was not found.
        echo Install Python 3.11+ from https://www.python.org/downloads/
        echo Make sure to check "Add Python to PATH" during install.
        echo.
        pause
        exit /b 1
    )
    set "PY=python"
)

echo Using Python launcher: %PY%
echo.

%PY% setup.py
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo [ERROR] Bootstrap failed. Scroll up for details.
    pause
    exit /b 1
)

echo.
echo Setup complete. See README.md for the next steps.
echo Press any key to close this window.
pause >nul
endlocal
