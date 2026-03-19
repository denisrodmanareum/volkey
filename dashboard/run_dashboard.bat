@echo off
setlocal
cd /d %~dp0

where python >nul 2>nul
if %errorlevel% neq 0 (
  echo Python not found. Please install Python 3 and add to PATH.
  pause
  exit /b 1
)

echo Starting dashboard at http://127.0.0.1:8787
python -m http.server 8787
