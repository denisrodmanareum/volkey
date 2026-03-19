@echo off
setlocal
cd /d %~dp0\..

REM 1) status json 1분 갱신 루프
start "VOLKY-STATUS-UPDATER" cmd /c "call dashboard\schedule_1m_update.bat"

REM 2) websocket 실시간 푸시 서버
start "VOLKY-WS-SERVER" cmd /c "call dashboard\run_ws_server.bat"

REM 3) 대시보드 HTTP 서버
cd dashboard
echo Dashboard: http://127.0.0.1:8787/?ws=ws://127.0.0.1:8765
python -m http.server 8787
