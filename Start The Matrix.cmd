@echo off
setlocal

where the-matrix >nul 2>nul
if errorlevel 1 (
    echo The Matrix is not installed yet.
    echo Run "Install The Matrix.cmd" first.
    echo.
    pause
    exit /b 1
)

the-matrix start

if errorlevel 1 (
    echo.
    echo The Matrix stopped with an error.
    pause
)
