@echo off
cd /d "%~dp0"

if not exist "src\upgrade_v02.py" (
  echo ERROR: src\upgrade_v02.py was not found.
  echo Please copy all files from this update package into the v0.1 folder.
  pause
  exit /b 1
)

if not exist "src\classify_streams.py" (
  echo ERROR: src\classify_streams.py was not found.
  pause
  exit /b 1
)

if not exist "src\generate_reports_v02.py" (
  echo ERROR: src\generate_reports_v02.py was not found.
  pause
  exit /b 1
)

py "src\upgrade_v02.py"
if errorlevel 1 goto :error

py "src\classify_streams.py"
if errorlevel 1 goto :error

py "src\generate_reports_v02.py"
if errorlevel 1 goto :error

echo.
echo v0.2 update completed successfully.
echo Open reports\dashboard.html
echo.
pause
exit /b 0

:error
echo.
echo ERROR: v0.2 update failed.
echo Please copy the entire error message and send it back.
echo.
pause
exit /b 1
