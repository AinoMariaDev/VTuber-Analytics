@echo off
chcp 65001 > nul
cd /d "%~dp0"
py src\generate_reports.py
echo.
pause
