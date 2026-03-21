#!/bin/bash
# 5분마다 대시보드 데이터를 GitHub Pages에 push
# crontab: */5 * * * * ~/Desktop/2026/volky-bot/scripts/push_dashboard_data.sh

set -e
cd "$(dirname "$0")/.."
DOCS=docs

# docs 폴더에 최신 데이터 복사
mkdir -p $DOCS/data $DOCS/papertrade

# status.json (포지션/PnL)
cp -f data/status.json $DOCS/data/status.json 2>/dev/null || true

# surge_status.json (MOIRAI 스캔)
cp -f data/surge_status.json $DOCS/data/surge_status.json 2>/dev/null || true

# 매매기록 (최근 500줄만)
tail -500 papertrade/scalp_live_trades.jsonl > $DOCS/papertrade/scalp_live_trades.jsonl 2>/dev/null || true

# 세션로그 (최근 200줄만)
tail -200 papertrade/scalp_live_session.log > $DOCS/papertrade/scalp_live_session.log 2>/dev/null || true

# 대시보드 HTML 최신본
cp -f dashboard/index.html $DOCS/index.html 2>/dev/null || true

# git push
cd "$(dirname "$0")/.."
git checkout gh-pages 2>/dev/null || git checkout -b gh-pages
git add docs/
if git diff --cached --quiet; then
    git checkout main 2>/dev/null
    exit 0
fi
git commit -m "data: update $(date '+%Y-%m-%d %H:%M')" --no-gpg-sign 2>/dev/null
git push origin gh-pages 2>/dev/null
git checkout main 2>/dev/null
