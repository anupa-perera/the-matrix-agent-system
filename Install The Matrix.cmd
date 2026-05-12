@echo off
setlocal

cd /d "%~dp0"

echo The Matrix installer
echo This will install The Matrix for this Windows user.
echo.

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*

if errorlevel 1 (
    echo.
    echo Installation failed.
    echo Please keep this window open and share the error text if you need help.
    pause
    exit /b %errorlevel%
)

echo.
echo The Matrix installer finished.
pause
