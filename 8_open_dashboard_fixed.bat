@echo off
cd /d "%~dp0"
if exist "reports\dashboard.html" (
  start "" "reports\dashboard.html"
) else (
  echo ERROR: reports\dashboard.html was not found.
  echo Run 7_v0.2_update_fixed.bat first.
  pause
)
