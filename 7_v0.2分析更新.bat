@echo off
chcp 65001 > nul
cd /d "%~dp0"
py src\upgrade_v02.py
py src\classify_streams.py
py src\generate_reports_v02.py
echo.
echo v0.2の分析更新が完了しました。
echo reports\dashboard.html を開いてください。
echo.
pause
