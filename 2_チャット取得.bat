@echo off
cd /d "%~dp0"
py "src\download_chats.py"
echo.
pause
