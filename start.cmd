@echo off
setlocal

where the-matrix >nul 2>nul
if errorlevel 1 (
    echo The Matrix is not installed yet.
    echo Run install.cmd first.
    exit /b 1
)

the-matrix start %*
exit /b %errorlevel%
