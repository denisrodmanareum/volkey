@echo off
setlocal
cd /d %~dp0\..

REM 1) Update status once
call dashboard\update_status.bat

REM 2) Start 1-min updater in another window
start "VOLKY-STATUS-UPDATER" cmd /c "call dashboard\schedule_1m_update.bat"

REM 3) Start dashboard server
cd dashboard
echo Dashboard: http://127.0.0.1:8787
python -m http.server 8787
