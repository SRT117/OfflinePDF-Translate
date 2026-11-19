@echo off
chcp 65001 >nul
cd /d "%~dp0"
if exist "runtime\python\pythonw.exe" (
    start "" "runtime\python\pythonw.exe" -B gui.py
) else (
    echo Missing portable Python runtime. See README.md for build instructions.
    pause
    exit /b 1
)
