@echo off
cd /d "%~dp0"

if not exist "app_config.local.json" (
  py "src\first_run_setup.py"
)

py "src\health_check.py"
if errorlevel 1 (
  echo.
  echo Health check failed.
  pause
  exit /b 1
)

start "" "http://127.0.0.1:8765"
py "src\web_app.py"
pause
