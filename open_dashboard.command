#!/bin/zsh

# volky-bot 대시보드 열기 (로컬 HTTP 서버)
# 정적 HTML이므로 간단한 Python HTTP 서버 사용

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
DASHBOARD_DIR="$ROOT_DIR/dashboard"
PORT="${VOLKY_DASH_PORT:-8765}"

# 기존 서버 종료
lsof -ti tcp:"$PORT" 2>/dev/null | xargs kill -9 2>/dev/null || true
sleep 0.5

echo "============================================================"
echo "  Volky-Bot Dashboard"
echo "============================================================"
echo
echo "  URL:  http://localhost:$PORT"
echo "  Dir:  $DASHBOARD_DIR"
echo
echo "  종료: Ctrl+C"
echo "============================================================"
echo

open "http://localhost:$PORT" &

cd "$DASHBOARD_DIR"
python3 -m http.server "$PORT"
