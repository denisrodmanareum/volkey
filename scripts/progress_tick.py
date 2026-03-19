#!/usr/bin/env python3
from __future__ import annotations
from datetime import datetime, timezone
from pathlib import Path
import json

BASE = Path('/Users/riot91naver.com/Desktop/2026/volky-bot')
STATUS = BASE / 'logs' / 'progress-status.json'
LOG = BASE / 'logs' / 'progress-tick.log'

STATUS.parent.mkdir(parents=True, exist_ok=True)
if not STATUS.exists():
    STATUS.write_text(json.dumps({"progress": 60, "stage": "multi-symbol+alerts wiring"}, ensure_ascii=False, indent=2), encoding='utf-8')

data = json.loads(STATUS.read_text(encoding='utf-8'))
now = datetime.now(timezone.utc).isoformat()
with LOG.open('a', encoding='utf-8') as f:
    f.write(f"[{now}] progress={data.get('progress')} stage={data.get('stage')}\n")
