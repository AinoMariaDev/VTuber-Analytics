@echo off
chcp 65001 > nul
cd /d "%~dp0"
py src\build_database.py
echo.
pause
