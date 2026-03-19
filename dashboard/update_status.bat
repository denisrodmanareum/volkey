@echo off
setlocal
cd /d %~dp0\..

set PY=python
where python >nul 2>nul
if %errorlevel% neq 0 (
  where py >nul 2>nul
  if %errorlevel% neq 0 (
    echo Python launcher not found.
    pause
    exit /b 1
  )
  set PY=py -3
)

%PY% scripts\export_dashboard_status.py
if %errorlevel% neq 0 (
  echo Failed to update status.json
  exit /b 1
)

echo Updated dashboard/data/status.json
