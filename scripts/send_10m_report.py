#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
import time
import hmac
import hashlib
from urllib.parse import urlencode
from datetime import datetime, timezone
from pathlib import Path

import requests
import yaml

BASE = Path('/Users/riot91naver.com/Desktop/2026/volky-bot')
STATE = BASE / 'papertrade' / 'scalp_live_state.json'
TRADES = BASE / 'papertrade' / 'scalp_live_trades.jsonl'
CFG = BASE / 'config' / 'scalping.yaml'
ENV = BASE / 'config' / '.env'
OUT = BASE / 'logs' / '10m-report.log'


def now_kst() -> str:
    return datetime.now(timezone.utc).astimezone().strftime('%H:%M:%S')


def mark_price(symbol: str, base_url: str) -> float | None:
    try:
        r = requests.get(f"{base_url}/fapi/v1/ticker/price", params={"symbol": symbol}, timeout=4)
        r.raise_for_status()
        return float(r.json().get('price'))
    except Exception:
        return None


def load_env(path: Path) -> dict:
    out = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        k, v = line.split('=', 1)
        out[k.strip()] = v.strip()
    return out


def signed_get(base_url: str, api_key: str, api_secret: str, endpoint: str, params: dict):
    p = dict(params)
    p['timestamp'] = int(time.time() * 1000)
    p['recvWindow'] = 5000
    qs = urlencode(p, doseq=True)
    sig = hmac.new(api_secret.encode(), qs.encode(), hashlib.sha256).hexdigest()
    url = f"{base_url}{endpoint}?{qs}&signature={sig}"
    r = requests.get(url, headers={'X-MBX-APIKEY': api_key}, timeout=6)
    r.raise_for_status()
    return r.json()


def income_sum_today(base_url: str, api_key: str, api_secret: str, income_type: str) -> float:
    if not api_key or not api_secret:
        return 0.0
    try:
        start_utc = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        data = signed_get(base_url, api_key, api_secret, '/fapi/v1/income', {
            'incomeType': income_type,
            'startTime': int(start_utc.timestamp() * 1000),
            'limit': 200
        })
        return float(sum(float(x.get('income', 0) or 0) for x in data))
    except Exception:
        return 0.0


cfg = yaml.safe_load(CFG.read_text(encoding='utf-8')) if CFG.exists() else {}
env = load_env(ENV)
base_url = env.get('BASE_URL', 'https://testnet.binancefuture.com')
api_key = env.get('API_KEY', '')
api_secret = env.get('API_SECRET', '')
leverage = cfg.get('leverage', '-')
margin_mode = cfg.get('margin_mode', 'ISOLATED')

entries = exits = 0
last = '-'
lines = []
last_entry_by_symbol = {}
if TRADES.exists():
    lines = [x for x in TRADES.read_text(encoding='utf-8').splitlines() if x.strip()]
    recent = lines[-80:]
    for ln in lines[-500:]:
        j = json.loads(ln)
        if j.get('type') == 'ENTRY':
            last_entry_by_symbol[j.get('symbol')] = j

open_positions = []
if STATE.exists():
    st = json.loads(STATE.read_text(encoding='utf-8'))
    for symbol, v in st.items():
        side = v.get('position')
        if side in ('LONG', 'SHORT'):
            entry = float(v.get('entryApprox') or v.get('entry') or 0)
            qty = float(v.get('qty') or 0)
            oid = v.get('orderId', '-')
            if qty <= 0 and symbol in last_entry_by_symbol:
                qty = float(last_entry_by_symbol[symbol].get('qty') or 0)
            mark = mark_price(symbol, base_url)
            pnl = None
            if mark and entry and qty:
                direction = 1 if side == 'LONG' else -1
                pnl = (mark - entry) * qty * direction
            open_positions.append({
                'symbol': symbol,
                'side': side,
                'entry': entry,
                'qty': qty,
                'orderId': oid,
                'mark': mark,
                'pnl': pnl,
            })

if TRADES.exists():
    recent = lines[-80:]
    for ln in recent:
        j = json.loads(ln)
        t = j.get('type')
        if t == 'ENTRY':
            entries += 1
        elif t == 'EXIT':
            exits += 1
    if lines:
        j = json.loads(lines[-1])
        last = f"{j.get('type')} {j.get('symbol')} {j.get('side', '')}"

total_unreal = 0.0
if open_positions:
    detail_lines = []
    for idx, p in enumerate(open_positions[:6], start=1):
        pnl_txt = f"{p['pnl']:+.2f}$" if p['pnl'] is not None else 'n/a'
        if p['pnl'] is not None:
            total_unreal += float(p['pnl'])
        detail_lines.append(
            f"[{idx}] {p['symbol']} ({p['side']})\n"
            f"  수량: {p['qty']:.6f}\n"
            f"  진입가: {p['entry']:.4f}\n"
            f"  현재가: {(p['mark'] or 0):.4f}\n"
            f"  미실현손익: {pnl_txt}\n"
            f"  주문ID: {p['orderId']}"
        )
    details = '\n\n'.join(detail_lines)
else:
    details = '- 없음'

# MD 기준 상태정보창 양식(한글)
real_order = bool(cfg.get('real_order', True))
if real_order:
    realized_today = income_sum_today(base_url, api_key, api_secret, 'REALIZED_PNL')
    commission_today = income_sum_today(base_url, api_key, api_secret, 'COMMISSION')
else:
    # 페이퍼 모드: 체결 로그 기반 금일 손익/수수료 추정
    realized_today = 0.0
    commission_today = 0.0
    fee_rate = float(cfg.get('fee_rate', 0.0004) or 0.0004)
    notional = float(cfg.get('order_notional_usdt', 40) or 40)
    lev = float(cfg.get('leverage', 3) or 3)
    per_trade_notional = notional * lev
    today_utc = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    for ln in lines:
        try:
            j = json.loads(ln)
        except Exception:
            continue
        if j.get('type') != 'EXIT':
            continue
        ts = j.get('ts')
        if not ts:
            continue
        try:
            dt = datetime.fromisoformat(str(ts).replace('Z', '+00:00'))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        except Exception:
            continue
        if dt < today_utc:
            continue
        realized_today += float(j.get('ret', 0) or 0) * per_trade_notional
        commission_today += -(per_trade_notional * fee_rate * 2.0)

net_realized_today = realized_today + commission_today
total_pnl = net_realized_today + total_unreal
engine_mode = 'PAPER loop running (mainnet market data)' if not real_order else 'LIVE loop running'

msg = (
    f"[10분 상태정보창 {now_kst()}]\n\n"
    f"[시스템 상태]\n"
    f"- 엔진: {engine_mode}\n"
    f"- 마진모드: {margin_mode}\n"
    f"- 레버리지: {leverage}x\n\n"
    f"[전략 상태]\n"
    f"- 최근 체결: ENTRY {entries} / EXIT {exits}\n"
    f"- 마지막 이벤트: {last}\n\n"
    f"[포지션 상태]\n"
    f"- 오픈포지션: {len(open_positions)}개\n"
    f"{details}\n\n"
    f"[실시간 손익]\n"
    f"- 미실현손익: {total_unreal:+.2f}$\n"
    f"- 금일 실현손익(체결): {realized_today:+.2f}$\n"
    f"- 금일 수수료: {commission_today:+.2f}$\n"
    f"- 금일 실현손익(수수료반영): {net_realized_today:+.2f}$\n"
    f"- 총 손익(실현+미실현, 수수료반영): {total_pnl:+.2f}$\n"
    f"- 코멘트: 실포지션 동기화 기준"
)

OUT.parent.mkdir(parents=True, exist_ok=True)
with OUT.open('a', encoding='utf-8') as f:
    f.write(msg + '\n---\n')

subprocess.run([
    '/usr/local/bin/node',
    '/Users/riot91naver.com/.npm-global/lib/node_modules/openclaw/dist/index.js',
    'message', 'send',
    '--channel', 'telegram',
    '--target', '1463388329',
    '--message', msg
], check=False)
