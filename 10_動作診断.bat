@echo off
cd /d "%~dp0"
py "src\health_check.py"
echo.
pause
