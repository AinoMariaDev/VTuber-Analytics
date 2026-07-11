@echo off
cd /d "%~dp0"

if not exist "data\vtuber_analytics.db" (
  echo ERROR: data\vtuber_analytics.db was not found.
  echo Run the database build first.
  pause
  exit /b 1
)

start "" "http://127.0.0.1:8765"
py "src\web_app.py"
pause
