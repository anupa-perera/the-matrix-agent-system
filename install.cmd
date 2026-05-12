@echo off
setlocal

set "INSTALL_PS1=%~dp0install.ps1"
set "INSTALL_PS1_URL=https://raw.githubusercontent.com/anupa-perera/the-matrix-agent-system/main/install.ps1"

cd /d "%~dp0"

if not exist "%INSTALL_PS1%" (
    set "INSTALL_PS1=%TEMP%\the-matrix-install.ps1"
    echo Downloading The Matrix installer from GitHub...

    where curl >nul 2>nul
    if not errorlevel 1 (
        curl -L --fail -o "%INSTALL_PS1%" "%INSTALL_PS1_URL%"
    ) else (
        powershell -NoProfile -ExecutionPolicy Bypass -Command "Invoke-RestMethod -Uri '%INSTALL_PS1_URL%' -OutFile '%INSTALL_PS1%'"
    )

    if errorlevel 1 (
        echo Failed to download The Matrix installer.
        echo Check your internet connection or GitHub access, then try again.
        exit /b 1
    )
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%INSTALL_PS1%" %*
exit /b %errorlevel%
