#!/bin/zsh
set -euo pipefail

BASE_DIR="/Users/riot91naver.com/Desktop/2026/volky-bot"
PYTHON_BIN="/Users/riot91naver.com/Desktop/2026/venv-chronos311/bin/python"

cd "$BASE_DIR"
mkdir -p papertrade logs

# 의존성 최소 설치(최초 1회)
"$PYTHON_BIN" -m pip install -q -r requirements.txt

exec "$PYTHON_BIN" scripts/paper_loop.py >> "$BASE_DIR/logs/paper-loop.out.log" 2>> "$BASE_DIR/logs/paper-loop.err.log"
