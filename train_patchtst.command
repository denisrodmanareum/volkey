#!/bin/zsh
# volky-bot PatchTST 학습 (메인 프로젝트 학습 스크립트 사용)
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
MAIN_DIR="$HOME/Desktop/2026"

if [ -x "$MAIN_DIR/venv-chronos311/bin/python" ]; then
  VENV="$MAIN_DIR/venv-chronos311"
elif [ -x "$MAIN_DIR/venv-mac/bin/python" ]; then
  VENV="$MAIN_DIR/venv-mac"
else
  echo "[ERROR] Python 가상환경을 찾을 수 없습니다."
  read -r -p "Press Enter to close..."
  exit 1
fi

echo "============================================================"
echo "  PatchTST 학습 — volky-bot 급등단타용"
echo "============================================================"
echo "  VENV:   $VENV"
echo "  OUTPUT: $MAIN_DIR/models/patchtst/"
echo

source "$VENV/bin/activate"
cd "$MAIN_DIR"

python scripts/train_patchtst_15m.py --all-timeframes --epochs 60

echo
echo "학습 완료! 모델: $MAIN_DIR/models/patchtst/"
read -r -p "Press Enter to close..."
