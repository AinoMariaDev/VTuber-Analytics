@echo off
chcp 65001 > nul
cd /d "%~dp0"
py src\status.py
echo.
pause
