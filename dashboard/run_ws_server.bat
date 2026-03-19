@echo off
setlocal
cd /d %~dp0\..

set PY=python
where python >nul 2>nul
if %errorlevel% neq 0 (
  where py >nul 2>nul
  if %errorlevel% neq 0 (
    echo Python not found.
    pause
    exit /b 1
  )
  set PY=py -3
)

%PY% -m pip install -q websockets >nul 2>nul
%PY% scripts\ws_status_server.py
