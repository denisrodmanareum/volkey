@echo off
setlocal
cd /d %~dp0\..

echo 1-min status updater started...
:loop
call dashboard\update_status.bat >nul 2>nul
timeout /t 60 /nobreak >nul
goto loop
