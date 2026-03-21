#!/usr/bin/env python3
from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urlencode

import pandas as pd
import requests
import yaml

BASE = Path('/Users/riot91naver.com/Desktop/2026/volky-bot')
sys.path.insert(0, str(BASE))

from engine.telegram_bot import notify_entry, notify_close, notify_daily_report
from strategies.scalp_breakout import load_strategy, signal, strategy_name, strategy_version

# AI Foundation Models (3-Layer Architecture)
_ai_manager = None
try:
    from engine.ai_manager import AIModelManager
except ImportError:
    AIModelManager = None

LOCK_FILE = BASE / 'papertrade' / 'scalp_live.lock'


def acquire_single_instance_lock() -> object:
    """동일 스크립트 중복 실행 방지. 락 파일 핸들을 반환."""
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    fh = open(LOCK_FILE, 'w')
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print(f"[ABORT] 다른 인스턴스가 이미 실행 중입니다 ({LOCK_FILE}). 종료합니다.")
        sys.exit(1)
    fh.write(str(os.getpid()))
    fh.flush()
    return fh

CFG = BASE / 'config' / 'scalping.yaml'
STRATEGY_CFG = BASE / 'config' / 'scalp_strategy.yaml'
ENV = BASE / 'config' / '.env'
LOG = BASE / 'papertrade' / 'scalp_live_session.log'
TRADES = BASE / 'papertrade' / 'scalp_live_trades.jsonl'
STATE = BASE / 'papertrade' / 'scalp_live_state.json'
STATUS = BASE / 'data' / 'status.json'


def now():
    return datetime.now(timezone.utc).isoformat()


def append(msg: str):
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open('a', encoding='utf-8') as f:
        f.write(f'[{now()}] {msg}\n')


def log_trade(ev: dict):
    TRADES.parent.mkdir(parents=True, exist_ok=True)
    with TRADES.open('a', encoding='utf-8') as f:
        f.write(json.dumps(ev, ensure_ascii=False) + '\n')


def load_env(path: Path) -> dict:
    out = {}
    for line in path.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        k, v = line.split('=', 1)
        out[k.strip()] = v.strip()
    return out


def load_cfg() -> dict:
    return yaml.safe_load(CFG.read_text(encoding='utf-8')) if CFG.exists() else {}


def load_status() -> dict:
    if not STATUS.exists():
        return {}
    try:
        return json.loads(STATUS.read_text(encoding='utf-8'))
    except Exception:
        return {}


def signed_request(base_url: str, key: str, secret: str, method: str, endpoint: str, params: dict):
    p = dict(params)
    p['timestamp'] = int(time.time() * 1000)
    p['recvWindow'] = 5000
    qs = urlencode(p, doseq=True)
    sig = hmac.new(secret.encode(), qs.encode(), hashlib.sha256).hexdigest()
    headers = {'X-MBX-APIKEY': key}
    url = f"{base_url}{endpoint}?{qs}&signature={sig}"
    r = requests.request(method, url, headers=headers, timeout=10)
    r.raise_for_status()
    return r.json()


_banned_symbols: set = {'LYNUSDT', 'XNYUSDT', 'BARDUSDT'}  # 문제 심볼 초기 차단
_ban_until: float = 0
_api_call_count: int = 0
_api_window_start: float = 0

def public_get(base_url: str, endpoint: str, params: dict | None = None):
    global _ban_until, _api_call_count, _api_window_start
    # IP 차단 중이면 대기
    if time.time() < _ban_until:
        time.sleep(max(_ban_until - time.time(), 0.1))
    # Rate limit: 분당 1200회 제한 (바이낸스 기본)
    now_t = time.time()
    if now_t - _api_window_start > 60:
        _api_call_count = 0
        _api_window_start = now_t
    _api_call_count += 1
    if _api_call_count > 1000:
        time.sleep(0.1)  # 속도 제한
    r = requests.get(f"{base_url}{endpoint}", params=params or {}, timeout=10)
    if r.status_code in (418, 429):
        sym = (params or {}).get('symbol', '')
        if sym:
            _banned_symbols.add(sym)
        if r.status_code == 418:
            _ban_until = time.time() + 120
        else:
            time.sleep(1)  # 429는 1초만 대기
        raise requests.exceptions.HTTPError(f"{r.status_code} banned sym={sym}")
    r.raise_for_status()
    return r.json()


def get_symbols(base_url: str) -> list[str]:
    ex = public_get(base_url, '/fapi/v1/exchangeInfo')
    syms = []
    for s in ex.get('symbols', []):
        if s.get('status') == 'TRADING' and s.get('contractType') == 'PERPETUAL' and s.get('quoteAsset') == 'USDT':
            syms.append(s['symbol'])
    return syms


def get_volatile_symbols(base_url: str, top_n: int = 30, min_quote_volume: float = 5_000_000) -> list[str]:
    """24h 변동률 기준 상위 top_n 심볼 반환. exchangeInfo로 실제 TRADING 심볼만 포함."""
    try:
        tickers = public_get(base_url, '/fapi/v1/ticker/24hr')
        ex = public_get(base_url, '/fapi/v1/exchangeInfo')
    except Exception:
        return []
    tradeable = {
        s['symbol'] for s in ex.get('symbols', [])
        if s.get('status') == 'TRADING' and s.get('contractType') == 'PERPETUAL'
    }
    scored = []
    for t in tickers:
        sym = t.get('symbol', '')
        if not sym.endswith('USDT') or sym not in tradeable:
            continue
        try:
            pct = abs(float(t.get('priceChangePercent', 0) or 0))
            qvol = float(t.get('quoteVolume', 0) or 0)
        except (ValueError, TypeError):
            continue
        if qvol < min_quote_volume:
            continue
        scored.append((sym, pct))
    scored.sort(key=lambda x: x[1], reverse=True)
    return [s for s, _ in scored[:top_n]]


def klines(base_url, symbol, interval, limit=120):
    raw = public_get(base_url, '/fapi/v1/klines', {'symbol': symbol, 'interval': interval, 'limit': limit})
    df = pd.DataFrame(raw, columns=['open_time', 'open', 'high', 'low', 'close', 'volume', 'close_time', 'qav', 'n', 'tb', 'tq', 'ig'])
    for c in ['open', 'high', 'low', 'close', 'volume', 'tb']:
        df[c] = pd.to_numeric(df[c], errors='coerce')
    return df


def execution_strength_ratio(df: pd.DataFrame, lookback: int = 12) -> float:
    d = df.tail(lookback)
    buy = float(d['tb'].sum() or 0)
    vol = float(d['volume'].sum() or 0)
    sell = max(vol - buy, 1e-9)
    return buy / sell


def open_interest(base_url: str, symbol: str) -> float:
    try:
        return float(public_get(base_url, '/fapi/v1/openInterest', {'symbol': symbol}).get('openInterest', 0))
    except Exception:
        return 0.0


def depth_wall_ratio(base_url: str, symbol: str, limit: int = 20) -> float:
    try:
        d = public_get(base_url, '/fapi/v1/depth', {'symbol': symbol, 'limit': limit})
        bids = d.get('bids', [])
        asks = d.get('asks', [])
        bid_wall = float(bids[0][1]) if bids else 0.0
        ask_wall = float(asks[0][1]) if asks else 0.0
        avg_bid = sum(float(x[1]) for x in bids[:10]) / max(min(len(bids), 10), 1)
        avg_ask = sum(float(x[1]) for x in asks[:10]) / max(min(len(asks), 10), 1)
        return max(bid_wall / max(avg_bid, 1e-9), ask_wall / max(avg_ask, 1e-9))
    except Exception:
        return 0.0


def spread_ratio(base_url: str, symbol: str) -> float:
    try:
        d = public_get(base_url, '/fapi/v1/ticker/bookTicker', {'symbol': symbol})
        bid = float(d.get('bidPrice', 0) or 0)
        ask = float(d.get('askPrice', 0) or 0)
        mid = (bid + ask) / 2 if (bid > 0 and ask > 0) else 0
        if mid <= 0:
            return 0.0
        return max(0.0, (ask - bid) / mid)
    except Exception:
        return 0.0


def price(base_url: str, symbol: str) -> float:
    return float(public_get(base_url, '/fapi/v1/ticker/price', {'symbol': symbol})['price'])


def step_size(base_url: str, symbol: str) -> float:
    ex = public_get(base_url, '/fapi/v1/exchangeInfo')
    for s in ex.get('symbols', []):
        if s.get('symbol') == symbol:
            for f in s.get('filters', []):
                if f.get('filterType') == 'LOT_SIZE':
                    return float(f['stepSize'])
    return 0.001


def quantize(qty: float, step: float) -> float:
    if step <= 0:
        return qty
    n = int(qty / step)
    return max(n * step, 0.0)


def ensure_mode(base_url, key, secret, symbol, margin_mode, leverage):
    try:
        signed_request(base_url, key, secret, 'POST', '/fapi/v1/marginType', {'symbol': symbol, 'marginType': margin_mode})
    except Exception:
        pass
    try:
        signed_request(base_url, key, secret, 'POST', '/fapi/v1/leverage', {'symbol': symbol, 'leverage': leverage})
    except Exception:
        pass


def htf_trend_ok(df_htf: pd.DataFrame, side: str) -> bool:
    if len(df_htf) < 60:
        return True
    ema20 = df_htf['close'].ewm(span=20, adjust=False).mean().iloc[-1]
    ema50 = df_htf['close'].ewm(span=50, adjust=False).mean().iloc[-1]
    if side == 'LONG':
        return ema20 >= ema50
    return ema20 <= ema50


def choch_bos_ok(df: pd.DataFrame, side: str, lookback: int = 20) -> bool:
    if len(df) < lookback + 3:
        return False
    prev = df.iloc[-2]
    highs = df['high'].iloc[-(lookback + 2):-2]
    lows = df['low'].iloc[-(lookback + 2):-2]
    if side == 'LONG':
        # BOS 유사: 직전 고점군 상향 이탈
        return float(prev['close']) > float(highs.max())
    return float(prev['close']) < float(lows.min())


def unicorn_overlap_ok(df: pd.DataFrame, side: str) -> bool:
    if len(df) < 8:
        return True
    # 간소화된 FVG 근사: 3캔들 불균형
    c0, c1, c2 = df.iloc[-3], df.iloc[-2], df.iloc[-1]
    bull_fvg = float(c2['low']) > float(c0['high'])
    bear_fvg = float(c2['high']) < float(c0['low'])
    # breaker 근사: 직전 캔들 몸통 반전 크기
    brk = abs(float(c1['close']) - float(c1['open'])) / max(float(c1['high']) - float(c1['low']), 1e-9)
    if side == 'LONG':
        return bull_fvg and brk > 0.45
    return bear_fvg and brk > 0.45


def otz_ok(df_htf: pd.DataFrame, side: str, lookback: int = 48) -> bool:
    """OTZ(Optimal Trade Zone): 추세 내 되돌림 구간인지 확인"""
    if len(df_htf) < lookback:
        return True
    d = df_htf.tail(lookback)
    hh = float(d['high'].max())
    ll = float(d['low'].min())
    if hh <= ll:
        return False
    px = float(d.iloc[-1]['close'])
    r = (px - ll) / (hh - ll)  # 0~1
    # LONG: 추세 내 눌림(0.62~0.79), SHORT: 되돌림(0.21~0.38)
    if side == 'LONG':
        return 0.62 <= r <= 0.79
    return 0.21 <= r <= 0.38


def ote_fvg_ok(df: pd.DataFrame, side: str) -> bool:
    """OTE + FVG 단순 근사: 비효율(FVG) 재진입 가능 구간"""
    if len(df) < 10:
        return False
    c0, c1, c2 = df.iloc[-3], df.iloc[-2], df.iloc[-1]
    bull_fvg = float(c2['low']) > float(c0['high'])
    bear_fvg = float(c2['high']) < float(c0['low'])
    # 최근 12봉 범위 기준 OTE 근사
    w = df.tail(12)
    hh = float(w['high'].max())
    ll = float(w['low'].min())
    if hh <= ll:
        return False
    px = float(c2['close'])
    r = (px - ll) / (hh - ll)
    if side == 'LONG':
        return bull_fvg and (0.55 <= r <= 0.79)
    return bear_fvg and (0.21 <= r <= 0.45)


def qm_bonus_ok(df: pd.DataFrame, side: str) -> bool:
    """QM(Quasimodo) 간략 근사: 변형 H&S 성격의 반전 구조"""
    if len(df) < 9:
        return False
    w = df.tail(9)
    h = list(w['high'])
    l = list(w['low'])
    # LONG: 저점 갱신 실패 + 고점 회복 / SHORT 반대
    if side == 'LONG':
        return (l[-5] < l[-7] and l[-5] < l[-3] and h[-2] > h[-4])
    return (h[-5] > h[-7] and h[-5] > h[-3] and l[-2] < l[-4])


def load_state(symbols):
    if STATE.exists():
        st = json.loads(STATE.read_text(encoding='utf-8'))
    else:
        st = {}
    for s in symbols:
        st.setdefault(s, {'position': 'FLAT', 'last_key': ''})
    return st


def fetch_open_positions(base_url, key, secret):
    arr = signed_request(base_url, key, secret, 'GET', '/fapi/v2/positionRisk', {})
    out = {}
    for p in arr:
        amt = float(p.get('positionAmt', 0) or 0)
        if abs(amt) <= 0:
            continue
        sym = p['symbol']
        out[sym] = {
            'position': 'LONG' if amt > 0 else 'SHORT',
            'qty': abs(amt),
            'entryApprox': float(p.get('entryPrice') or 0),
            'markPrice': float(p.get('markPrice') or 0),
            'unRealizedProfit': float(p.get('unRealizedProfit') or 0),
        }
    return out


def reconcile_state_with_exchange(st, symbols, exch_open):
    # exchange positionRisk를 단일 진실 소스로 사용
    for s in symbols:
        st.setdefault(s, {'position': 'FLAT', 'last_key': ''})
        if s in exch_open:
            ex = exch_open[s]
            st[s]['position'] = ex['position']
            st[s]['qty'] = ex['qty']
            st[s]['entryApprox'] = ex['entryApprox']
        else:
            # last_key/last_exit_ts는 유지해서 재진입 쿨다운/중복신호 방지
            st[s]['position'] = 'FLAT'
            st[s].pop('qty', None)
            st[s].pop('entryApprox', None)
            st[s].pop('orderId', None)
            st[s].pop('peak', None)
            st[s].pop('trough', None)


def save_state(st):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding='utf-8')
    # status.json 업데이트 (대시보드용)
    try:
        positions = []
        total_pnl = 0.0
        for sym, v in st.items():
            if not isinstance(v, dict) or v.get('position') not in ('LONG', 'SHORT'):
                continue
            entry_px = float(v.get('entryApprox', 0) or 0)
            mark_px = float(v.get('peak', entry_px) or entry_px)  # 근사값
            qty = float(v.get('qty', 0) or 0)
            side = v['position']
            if side == 'LONG':
                upnl = (mark_px - entry_px) * qty if entry_px > 0 else 0
            else:
                upnl = (entry_px - mark_px) * qty if entry_px > 0 else 0
            total_pnl += upnl
            positions.append({
                'symbol': sym, 'side': side, 'qty': qty,
                'entry': entry_px, 'mark': mark_px,
                'unrealized_pnl': round(upnl, 4),
                'strategy': v.get('strategy_name', ''),
            })
        # 누적 실현 PnL 계산
        realized_gross = 0.0
        total_commission = 0.0
        try:
            if TRADES.exists():
                for line in TRADES.read_text().strip().split('\n')[-500:]:
                    t = json.loads(line)
                    if t.get('type') == 'EXIT':
                        ret = float(t.get('ret', 0) or 0)
                        notional = 40 * 3  # notional * leverage
                        realized_gross += ret * notional
                        total_commission += fee_rate * 2 * notional  # 왕복 수수료
        except Exception:
            pass
        realized_net = realized_gross - total_commission
        status_data = {
            'updated_at': datetime.now(timezone(timedelta(hours=9))).strftime('%Y-%m-%dT%H:%M:%S+0900'),
            'config': {
                'margin_mode': 'ISOLATED',
                'leverage': 3,
                'max_positions': 6,
                'order_notional_usdt': 40,
            },
            'pnl': {
                'realized_gross': round(realized_gross, 4),
                'commission': round(total_commission, 4),
                'realized_net': round(realized_net, 4),
                'unrealized': round(total_pnl, 4),
                'total': round(realized_net + total_pnl, 4),
            },
            'positions': positions,
        }
        STATUS.parent.mkdir(parents=True, exist_ok=True)
        STATUS.write_text(json.dumps(status_data, ensure_ascii=False, indent=2), encoding='utf-8')
    except Exception:
        pass


def place_market_order(base_url, key, secret, symbol, side, qty, reduce_only=False, real_order=True):
    if not real_order:
        return {
            'orderId': int(time.time() * 1000),
            'symbol': symbol,
            'side': side,
            'status': 'FILLED',
            'type': 'MARKET',
            'reduceOnly': bool(reduce_only),
            'paper': True,
        }
    params = {
        'symbol': symbol,
        'side': side,
        'type': 'MARKET',
        'quantity': f"{qty:.8f}".rstrip('0').rstrip('.')
    }
    if reduce_only:
        params['reduceOnly'] = 'true'
    return signed_request(base_url, key, secret, 'POST', '/fapi/v1/order', params)


def main():
    _lock_fh = acquire_single_instance_lock()  # 중복 실행 차단
    cfg = load_cfg()
    cfg_mtime = CFG.stat().st_mtime if CFG.exists() else 0.0
    strategy_cfg = load_strategy(STRATEGY_CFG)
    strategy_mtime = STRATEGY_CFG.stat().st_mtime if STRATEGY_CFG.exists() else 0.0
    strategy_meta = {"name": strategy_name(strategy_cfg), "version": strategy_version(strategy_cfg)}
    env = load_env(ENV)
    base_url = env.get('BASE_URL', 'https://testnet.binancefuture.com')
    key = env['API_KEY']
    secret = env['API_SECRET']

    volatile_top_n = 30
    volatile_refresh_minutes = 30
    volatile_min_qvol = 5_000_000.0
    intervals = ['1m', '3m', '5m']
    htf_interval = '1h'
    max_positions = 3
    notional = 30.0
    margin_mode = 'ISOLATED'
    leverage = 3
    base_sl = 0.006
    base_tp1 = 0.008
    base_tp2 = 0.016
    fee_rate = 0.0004
    entry_min_volume_mult = 2.8
    entry_min_body_ratio = 0.35
    reentry_cooldown_sec = 600
    max_hold_minutes = 12
    global_entry_cooldown_sec = 10
    loop_sleep_sec = 0.5
    max_consecutive_losses_cfg = 3
    cooldown_minutes = 30
    enable_choch_bos_filter = True
    enable_unicorn_filter = True
    enable_otz_filter = True
    enable_ote_filter = True
    enable_qm_bonus = True
    enable_execution_strength_filter = True
    enable_open_interest_filter = True
    enable_spoofing_filter = True
    min_exec_strength_ratio = 1.15
    max_oi_spike_ratio = 1.08
    spoof_wall_ratio_threshold = 2.8
    entry_confirm_secs = 2          # 즉사 SL 방지: 진입 확인 대기 시간(초)
    max_entry_slip_pct = 0.002      # 즉사 SL 방지: 트리거 대비 허용 역방향 슬리피지
    max_chase_pct = 0.004           # 추격 진입 방지: 트리거 대비 최대 유리 방향 초과 허용
    stagnation_minutes = 3.0        # 조기 스태그네이션 탈출: N분 + 손실 중 조기 퇴장
    soft_timeout_min_profit_pct = 0.003  # 소프트 타임아웃 익절은 최소 이익 이상일 때만
    max_dyn_sl_mult = 1.4           # 동적 손절 상한 배수
    min_rr_ratio = 2.0              # 최소 R:R 비율 강제 (dyn_tp >= dyn_sl × min_rr_ratio)
    real_order = True                # false면 실거래 기반 페이퍼(가상체결)
    blocked_hours_kst: set[int] = set()   # 손실 다발 시간대 진입 제한
    loss_symbol_cooldown_minutes = 180     # 손실 심볼 재진입 쿨다운
    min_protect_secs = 10                  # 진입 직후 즉시 SL 방지 최소 보호시간
    early_sl_buffer_mult = 1.25            # 보호시간 동안 SL 완충 배수
    min_entry_votes = 2                    # 진입 합의 점수 최소치(2-of-N)
    max_spread_ratio = 0.0015              # 최대 허용 스프레드 비율
    spread_grace_secs = 20                 # 진입 직후 스프레드 급확장 유예 구간

    def apply_runtime_config(new_cfg: dict):
        nonlocal cfg
        nonlocal volatile_top_n, volatile_refresh_minutes, volatile_min_qvol
        nonlocal intervals, htf_interval, max_positions, notional, margin_mode, leverage
        nonlocal base_sl, base_tp1, base_tp2, fee_rate, entry_min_volume_mult, entry_min_body_ratio
        nonlocal reentry_cooldown_sec, max_hold_minutes, global_entry_cooldown_sec, loop_sleep_sec
        nonlocal max_consecutive_losses_cfg, cooldown_minutes
        nonlocal enable_choch_bos_filter, enable_unicorn_filter
        nonlocal enable_otz_filter, enable_ote_filter, enable_qm_bonus
        nonlocal enable_execution_strength_filter, enable_open_interest_filter, enable_spoofing_filter
        nonlocal min_exec_strength_ratio, max_oi_spike_ratio, spoof_wall_ratio_threshold
        nonlocal entry_confirm_secs, max_entry_slip_pct
        nonlocal max_chase_pct, stagnation_minutes, soft_timeout_min_profit_pct, max_dyn_sl_mult, min_rr_ratio
        nonlocal real_order, blocked_hours_kst, loss_symbol_cooldown_minutes, min_protect_secs, early_sl_buffer_mult
        nonlocal min_entry_votes, max_spread_ratio, spread_grace_secs
        cfg = new_cfg
        volatile_top_n = int(cfg.get('volatile_top_n', 30))
        volatile_refresh_minutes = int(cfg.get('volatile_refresh_minutes', 30))
        volatile_min_qvol = float(cfg.get('volatile_min_quote_volume', 5_000_000))
        intervals = cfg.get('intervals', ['1m', '3m', '5m'])
        htf_interval = cfg.get('htf_interval', '1h')
        max_positions = int(cfg.get('max_positions', 3))
        notional = float(cfg.get('order_notional_usdt', 30))
        margin_mode = cfg.get('margin_mode', 'ISOLATED').upper()
        leverage = int(cfg.get('leverage', 3))
        base_sl = float(cfg.get('sl_pct', 0.006))
        base_tp1 = float(cfg.get('tp1_pct', 0.008))
        base_tp2 = float(cfg.get('tp2_pct', 0.016))
        fee_rate = float(cfg.get('fee_rate', 0.0004))
        entry_min_volume_mult = float(cfg.get('entry_min_volume_mult', 2.8))
        entry_min_body_ratio = float(cfg.get('entry_min_body_ratio', 0.35))
        reentry_cooldown_sec = int(cfg.get('reentry_cooldown_sec', 600))
        max_hold_minutes = int(cfg.get('max_hold_minutes', 12))
        global_entry_cooldown_sec = int(cfg.get('global_entry_cooldown_sec', 10))
        loop_sleep_sec = float(cfg.get('loop_sleep_sec', 0.5))
        max_consecutive_losses_cfg = int(cfg.get('max_consecutive_losses', 3))
        cooldown_minutes = int(cfg.get('cooldown_minutes', 30))
        enable_choch_bos_filter = bool(cfg.get('enable_choch_bos_filter', True))
        enable_unicorn_filter = bool(cfg.get('enable_unicorn_filter', True))
        enable_otz_filter = bool(cfg.get('enable_otz_filter', True))
        enable_ote_filter = bool(cfg.get('enable_ote_filter', True))
        enable_qm_bonus = bool(cfg.get('enable_qm_bonus', True))
        enable_execution_strength_filter = bool(cfg.get('enable_execution_strength_filter', True))
        enable_open_interest_filter = bool(cfg.get('enable_open_interest_filter', True))
        enable_spoofing_filter = bool(cfg.get('enable_spoofing_filter', True))
        min_exec_strength_ratio = float(cfg.get('min_exec_strength_ratio', 1.15))
        max_oi_spike_ratio = float(cfg.get('max_oi_spike_ratio', 1.08))
        spoof_wall_ratio_threshold = float(cfg.get('spoof_wall_ratio_threshold', 2.8))
        entry_confirm_secs = int(cfg.get('entry_confirm_secs', 2))
        max_entry_slip_pct = float(cfg.get('max_entry_slip_pct', 0.002))
        max_chase_pct = float(cfg.get('max_chase_pct', 0.004))
        stagnation_minutes = float(cfg.get('stagnation_minutes', 3.0))
        soft_timeout_min_profit_pct = float(cfg.get('soft_timeout_min_profit_pct', 0.003))
        max_dyn_sl_mult = float(cfg.get('max_dyn_sl_mult', 1.4))
        min_rr_ratio = float(cfg.get('min_rr_ratio', 2.0))
        real_order = bool(cfg.get('real_order', True))
        blocked_hours_kst = set(int(h) for h in cfg.get('blocked_hours_kst', []) if str(h).isdigit())
        loss_symbol_cooldown_minutes = int(cfg.get('loss_symbol_cooldown_minutes', 180))
        min_protect_secs = int(cfg.get('min_protect_secs', 10))
        early_sl_buffer_mult = float(cfg.get('early_sl_buffer_mult', 1.25))
        min_entry_votes = int(cfg.get('min_entry_votes', 2))
        max_spread_ratio = float(cfg.get('max_spread_ratio', 0.0015))
        spread_grace_secs = int(cfg.get('spread_grace_secs', 20))

    apply_runtime_config(cfg)

    if cfg.get('scan_all_symbols', False):
        symbols = get_volatile_symbols(base_url, top_n=volatile_top_n, min_quote_volume=volatile_min_qvol)
        if not symbols:
            symbols = get_symbols(base_url)[:volatile_top_n]
    else:
        symbols = cfg.get('symbols', [])
    symbol_limit = int(cfg.get('symbol_limit', 0) or 0)
    if symbol_limit > 0:
        symbols = symbols[:symbol_limit]

    last_symbol_refresh = time.time()
    st = load_state(symbols)
    append(f"start live testnet symbols={len(symbols)} intervals={intervals} max_positions={max_positions}")

    # ── AI Foundation Models (3-Layer) ──
    global _ai_manager
    ai_cfg = cfg.get('ai_models', {})
    if AIModelManager and ai_cfg.get('enabled', False):
        _ai_manager = AIModelManager(ai_cfg)
        ai_status = _ai_manager.load_all()
        append(f"AI_MODELS_LOADED {ai_status}")
    else:
        append("AI_MODELS_DISABLED (ai_models.enabled=false or import failed)")
    last_moirai_scan_ts: float = 0

    last_global_entry_ts: int = 0   # 전체 마지막 진입 시각 (버스트 방지)
    consecutive_losses: int = 0     # 연속 SL 카운터
    cooldown_until: int = 0         # 연속 손실 쿨다운 종료 시각
    pending_signals: dict = {}      # 즉사 SL 방지: 진입 확인 대기 (2-pass)
    symbol_cooldown_until: dict[str, int] = {}  # 심볼별 재진입 쿨다운
    halt_logged = False

    while True:
        try:
            current_mtime = CFG.stat().st_mtime if CFG.exists() else 0.0
            if current_mtime and current_mtime != cfg_mtime:
                apply_runtime_config(load_cfg())
                cfg_mtime = current_mtime
                append(
                    "CONFIG_RELOADED "
                    f"sl={base_sl} tp1={base_tp1} tp2={base_tp2} "
                    f"vol_spike={cfg.get('volume_spike_mult', 2.5)} "
                    f"entry_vol={entry_min_volume_mult}"
                )

            current_strategy_mtime = STRATEGY_CFG.stat().st_mtime if STRATEGY_CFG.exists() else 0.0
            if current_strategy_mtime != strategy_mtime:
                strategy_cfg = load_strategy(STRATEGY_CFG)
                strategy_mtime = current_strategy_mtime
                strategy_meta = {"name": strategy_name(strategy_cfg), "version": strategy_version(strategy_cfg)}
                append(
                    "STRATEGY_RELOADED "
                    f"name={strategy_meta['name']} "
                    f"version={strategy_meta['version']} "
                    f"enabled={strategy_cfg.get('enabled', True)}"
                )

            status = load_status()
            trading_halted = bool(status.get('trading_halt'))
            if trading_halted and not halt_logged:
                append('TRADING_HALTED status.json trading_halt=true')
                halt_logged = True
            elif not trading_halted and halt_logged:
                append('TRADING_RESUMED status.json trading_halt=false')
                halt_logged = False

            # 30분마다 변동률 기준으로 심볼 재선정
            if cfg.get('scan_all_symbols', False) and (time.time() - last_symbol_refresh) >= volatile_refresh_minutes * 60:
                new_syms = get_volatile_symbols(base_url, top_n=volatile_top_n, min_quote_volume=volatile_min_qvol)
                if new_syms:
                    added = [s for s in new_syms if s not in symbols]
                    removed = [s for s in symbols if s not in new_syms]
                    symbols = new_syms
                    for s in added:
                        st.setdefault(s, {'position': 'FLAT', 'last_key': ''})
                    last_symbol_refresh = time.time()
                    append(f"symbol_refresh symbols={len(symbols)} added={len(added)} removed={len(removed)} top={symbols[:5]}")

            # ── MOIRAI-2 배치 스캔 (10분 간격, 비동기) ─────────────
            moirai_interval = float(ai_cfg.get('moirai_scan_interval', 600))
            if _ai_manager is not None and (time.time() - last_moirai_scan_ts) >= moirai_interval:
                import threading
                def _moirai_bg_scan():
                    try:
                        from concurrent.futures import ThreadPoolExecutor, as_completed
                        all_futures_syms = [s for s in get_symbols(base_url) if s not in _banned_symbols]
                        append(f"MOIRAI_SCAN_START total_symbols={len(all_futures_syms)}")
                        def _fetch_closes(sym):
                            try:
                                _mk = requests.get(f"{base_url}/fapi/v1/klines",
                                    params={"symbol": sym, "interval": "5m", "limit": 64}, timeout=5).json()
                                if isinstance(_mk, list) and len(_mk) >= 32:
                                    return sym, [float(k[4]) for k in _mk]
                            except Exception:
                                pass
                            return sym, None
                        coin_closes_bg: dict[str, list[float]] = {}
                        # 배치 처리: 50개씩 끊어서 rate limit 방지
                        batch_size = 50
                        for bi in range(0, len(all_futures_syms), batch_size):
                            batch = all_futures_syms[bi:bi + batch_size]
                            with ThreadPoolExecutor(max_workers=5) as pool:
                                futs = {pool.submit(_fetch_closes, s): s for s in batch}
                                for fut in as_completed(futs, timeout=30):
                                    try:
                                        sym, cd = fut.result()
                                        if cd is not None:
                                            coin_closes_bg[sym] = cd
                                    except Exception:
                                        pass
                            if bi + batch_size < len(all_futures_syms):
                                time.sleep(1)  # 배치 간 1초 대기
                        if coin_closes_bg:
                            candidates = _ai_manager.run_moirai_scan_sync(coin_closes_bg)
                            surge_data = {
                                "last_scan": datetime.now(timezone.utc).isoformat(),
                                "total_scanned": len(coin_closes_bg),
                                "candidates": [
                                    {"symbol": c.symbol, "anomaly_score": round(c.anomaly_score, 4),
                                     "predicted_return": round(c.predicted_return, 6),
                                     "q10": round(c.q10, 6), "q50": round(c.q50, 6), "q90": round(c.q90, 6)}
                                    for c in candidates
                                ],
                            }
                            surge_path = BASE / 'data' / 'surge_status.json'
                            surge_path.parent.mkdir(parents=True, exist_ok=True)
                            surge_path.write_text(json.dumps(surge_data, indent=2))
                            append(f"MOIRAI_SCAN scanned={len(coin_closes_bg)} candidates={len(candidates)} top={[c.symbol for c in candidates[:3]]}")
                            # MOIRAI 상위 후보를 진입 대상에 자동 합류
                            moirai_top_n = int(ai_cfg.get('moirai_inject_top_n', 10))
                            min_anomaly = float(ai_cfg.get('moirai_min_anomaly', 0.20))
                            injected = []
                            for c in candidates[:moirai_top_n]:
                                if c.anomaly_score >= min_anomaly and c.symbol not in symbols:
                                    symbols.append(c.symbol)
                                    st.setdefault(c.symbol, {'position': 'FLAT', 'last_key': ''})
                                    injected.append(f"{c.symbol}({c.anomaly_score:.2f})")
                            if injected:
                                append(f"MOIRAI_INJECT added={len(injected)} symbols={injected} total={len(symbols)}")
                    except Exception as e:
                        append(f"MOIRAI_SCAN_ERROR {e}")
                threading.Thread(target=_moirai_bg_scan, daemon=True).start()
                last_moirai_scan_ts = time.time()

            exch_open = fetch_open_positions(base_url, key, secret) if real_order else {}
            # 현재 오픈 포지션 심볼은 스캔 목록에서 빠져도 반드시 관리 대상에 포함
            active_symbols = list(dict.fromkeys(list(symbols) + list(exch_open.keys())))
            if real_order:
                reconcile_state_with_exchange(st, active_symbols, exch_open)
            save_state(st)
            open_count = sum(1 for v in st.values() if v.get('position') in ('LONG', 'SHORT'))

            # ── 즉사 SL 방지: 2-pass 진입 확인 ─────────────────────────────────
            _to_rm: list = []
            for _ps, _pend in list(pending_signals.items()):
                _age = time.time() - _pend['ts']
                if _age > 30:  # 30초 초과 → 만료 폐기
                    append(f"PENDING_EXPIRED {_ps} {_pend['sig']} age={_age:.1f}s")
                    _to_rm.append(_ps)
                    continue
                if _age < entry_confirm_secs:
                    continue  # 확인 대기 시간 미충족 → 다음 루프
                if st.get(_ps, {}).get('position') in ('LONG', 'SHORT') or open_count >= max_positions:
                    _to_rm.append(_ps)
                    continue
                if trading_halted or int(time.time()) < cooldown_until:
                    _to_rm.append(_ps)
                    continue
                try:
                    _cur_px = price(base_url, _ps)
                except Exception:
                    _to_rm.append(_ps)
                    continue
                _psig = _pend['sig']
                _tpx = _pend['trigger_px']
                # 방향 반전 체크: 트리거 대비 max_entry_slip_pct 초과 역행 시 취소
                if _psig == 'LONG' and _cur_px < _tpx * (1.0 - max_entry_slip_pct):
                    _slip = (_tpx - _cur_px) / _tpx * 100
                    append(f"PENDING_CANCEL {_ps} {_psig} reversed tpx={_tpx:.6g} now={_cur_px:.6g} slip={_slip:.3f}%")
                    _to_rm.append(_ps)
                    continue
                if _psig == 'SHORT' and _cur_px > _tpx * (1.0 + max_entry_slip_pct):
                    _slip = (_cur_px - _tpx) / _tpx * 100
                    append(f"PENDING_CANCEL {_ps} {_psig} reversed tpx={_tpx:.6g} now={_cur_px:.6g} slip={_slip:.3f}%")
                    _to_rm.append(_ps)
                    continue
                # 추격 진입 방지: 유리 방향으로 너무 많이 이미 움직인 경우 (모멘텀 소진 가능성)
                if _psig == 'LONG' and _cur_px > _tpx * (1.0 + max_chase_pct):
                    _over = (_cur_px - _tpx) / _tpx * 100
                    append(f"PENDING_CANCEL {_ps} {_psig} chased tpx={_tpx:.6g} now={_cur_px:.6g} overshoot={_over:.3f}%")
                    _to_rm.append(_ps)
                    continue
                if _psig == 'SHORT' and _cur_px < _tpx * (1.0 - max_chase_pct):
                    _over = (_tpx - _cur_px) / _tpx * 100
                    append(f"PENDING_CANCEL {_ps} {_psig} chased tpx={_tpx:.6g} now={_cur_px:.6g} overshoot={_over:.3f}%")
                    _to_rm.append(_ps)
                    continue
                # 글로벌 진입 쿨다운 아직이면 다음 루프에서 재시도
                if (int(time.time()) - last_global_entry_ts) < global_entry_cooldown_sec:
                    continue
                # ── Layer 3: Lag-Llama Risk Gate ──────────────────────────────
                _entry_notional = notional
                if _ai_manager is not None:
                    try:
                        _df_risk = klines(base_url, _ps, '5m', limit=120)
                        _closes_risk = [float(r['close']) for _, r in _df_risk.iterrows()] if _df_risk is not None and len(_df_risk) > 32 else []
                        if _closes_risk:
                            _risk = _ai_manager.check_risk(_closes_risk, '5m')
                            if _risk is not None:
                                _blocked, _breason = _risk.should_block(
                                    float(ai_cfg.get('min_risk_reward', 1.5)),
                                    float(ai_cfg.get('min_kelly_fraction', 0.03)),
                                )
                                if _blocked:
                                    append(f"RISK_BLOCKED {_ps} {_psig} reason={_breason}")
                                    _to_rm.append(_ps)
                                    continue
                                # Kelly-adjusted position size
                                _entry_notional = _risk.position_size_usdt(notional)
                                if abs(_entry_notional - notional) > 1.0:
                                    append(f"KELLY_SIZE {_ps} base={notional:.0f} adj={_entry_notional:.0f} kelly={_risk.kelly_fraction:.3f}")
                    except Exception as e:
                        append(f"RISK_CHECK_ERROR {_ps}: {e}")

                # ── 진입 확정 ─────────────────────────────────────────────────
                _piv = _pend['iv']
                _pstep = step_size(base_url, _ps)
                _pqty = quantize((_entry_notional * leverage) / max(_cur_px, 1e-9), _pstep)
                if _pqty <= 0:
                    _to_rm.append(_ps)
                    continue
                try:
                    if real_order:
                        ensure_mode(base_url, key, secret, _ps, margin_mode, leverage)
                    _porder = place_market_order(
                        base_url, key, secret, _ps,
                        'BUY' if _psig == 'LONG' else 'SELL',
                        _pqty, reduce_only=False, real_order=real_order
                    )
                    st[_ps]['position'] = _psig
                    st[_ps]['orderId'] = _porder.get('orderId')
                    st[_ps]['entryApprox'] = _cur_px
                    st[_ps]['qty'] = _pqty
                    st[_ps]['strategy_name'] = strategy_meta['name']
                    st[_ps]['strategy_version'] = strategy_meta['version']
                    st[_ps]['peak'] = _cur_px
                    st[_ps]['trough'] = _cur_px
                    st[_ps]['last_entry_ts'] = int(time.time())
                    last_global_entry_ts = int(time.time())
                    open_count += 1
                    append(
                        f"ENTRY(confirmed) {_ps} {_psig} iv={_piv} qty={_pqty} "
                        f"tpx={_tpx:.6g} now={_cur_px:.6g} age={_age:.1f}s "
                        f"orderId={_porder.get('orderId')}"
                    )
                    log_trade({
                        'ts': now(), 'symbol': _ps, 'iv': _piv, 'type': 'ENTRY',
                        'side': _psig, 'qty': _pqty, 'orderId': _porder.get('orderId'),
                        'volume_mult': round(_pend.get('vol_mult', 0), 3),
                        'body_ratio': round(_pend.get('body_ratio', 0), 3),
                        'strategy': strategy_meta['name'],
                        'strategy_version': strategy_meta['version'],
                    })
                    # 텔레그램 알림
                    try:
                        notify_entry(_ps, _psig.lower(), _cur_px, 0, _tpx, 'surge', 0, strategy_meta['name'], dry=not real_order)
                    except Exception:
                        pass
                except Exception as _pex:
                    append(f"ENTRY_ERROR(confirmed) {_ps} {_psig}: {_pex}")
                _to_rm.append(_ps)
            for _ps in _to_rm:
                pending_signals.pop(_ps, None)
            # ─────────────────────────────────────────────────────────────────

            for sym in active_symbols:
                if sym in _banned_symbols:
                    continue
                if open_count >= max_positions and st[sym].get('position') == 'FLAT':
                    continue
                # 손실 심볼 재진입 쿨다운
                if st[sym].get('position') == 'FLAT':
                    until = int(symbol_cooldown_until.get(sym, 0))
                    if until > int(time.time()):
                        continue
                for iv in intervals:
                    df = klines(base_url, sym, iv)
                    px = float(df.iloc[-1]['close'])

                    # dynamic stop/tp + trailing on open position (evaluate on 1m only)
                    if st[sym].get('position') in ('LONG', 'SHORT') and iv == intervals[0]:
                        pos = st[sym]['position']
                        entry = float(st[sym].get('entryApprox', px))
                        qty = float(st[sym].get('qty', 0))
                        if qty > 0 and entry > 0:
                            vol = float(df['close'].pct_change().tail(20).std() or 0)
                            dyn_sl = max(base_sl * 0.6, min(base_sl * max_dyn_sl_mult, vol * 3.0))
                            dyn_tp = max(base_tp2 * 0.7, min(base_tp2 * 1.5, dyn_sl * 2.2))
                            # 최소 R:R 강제: dyn_tp >= dyn_sl × min_rr_ratio
                            dyn_tp = max(dyn_tp, dyn_sl * min_rr_ratio)

                            direction = 1 if pos == 'LONG' else -1
                            ret_gross = (px - entry) / entry * direction
                            round_trip_fee_cost = fee_rate * 2  # 0.08%
                            ret = ret_gross - round_trip_fee_cost  # 수수료 차감된 순수익률

                            # trailing reference
                            if pos == 'LONG':
                                st[sym]['peak'] = max(float(st[sym].get('peak', entry)), px)
                                trail_ret = (px - float(st[sym]['peak'])) / float(st[sym]['peak'])
                            else:
                                st[sym]['trough'] = min(float(st[sym].get('trough', entry)), px)
                                trail_ret = (float(st[sym]['trough']) - px) / float(st[sym]['trough'])

                            round_trip_fee = round_trip_fee_cost  # 이미 위에서 계산
                            # be_lock: TP1 + 수수료 이상 수익일 때만 본절 보호 의미 있음
                            be_lock = ret >= max(base_tp1 + round_trip_fee, dyn_sl * 0.8)
                            stop_level = -dyn_sl
                            if be_lock:
                                # 수수료 포함 실질 본절 보호 (최소 +0.08% 이상 확보)
                                stop_level = max(stop_level, round_trip_fee)

                            age_sec = int(time.time()) - int(st[sym].get('last_entry_ts', int(time.time())))
                            hold_min = age_sec / 60.0
                            # 소프트 타임아웃(max_hold_minutes): 수수료 이상 손익 확정 시에만 종료
                            # 하드 타임아웃(×2): 수수료 미달 구간에서도 강제 종료
                            soft_timeout = hold_min >= max_hold_minutes
                            hard_timeout = hold_min >= max_hold_minutes * 2
                            soft_timeout_take = soft_timeout and ret >= max(soft_timeout_min_profit_pct, round_trip_fee)
                            soft_timeout_cut = soft_timeout and ret <= -max(round_trip_fee, dyn_sl * 0.35)
                            timeout_hit = hard_timeout or soft_timeout_take or soft_timeout_cut

                            # 조기 스태그네이션 탈출: N분 경과 + 수수료 이상 손실 + TP 터치 이력 없음
                            # → 방향 없이 손실 중인 포지션을 빠르게 정리
                            stagnation_hit = (
                                hold_min >= stagnation_minutes
                                and not be_lock
                                and ret <= -round_trip_fee
                            )

                            # 진입 직후 보호: 짧은 구간은 SL 완충 + 즉시청산 방지
                            effective_stop = stop_level
                            if age_sec < max(min_protect_secs, 0):
                                effective_stop = min(effective_stop, stop_level * early_sl_buffer_mult)
                            stop_hit = (age_sec >= max(min_protect_secs, 0) and ret <= effective_stop) or (ret <= (stop_level * 2.0))
                            # 진입 직후 스프레드 급확장 구간은 SL 1틱 유예
                            if stop_hit and age_sec < max(spread_grace_secs, 0):
                                sr_now = spread_ratio(base_url, sym)
                                if sr_now > (max_spread_ratio * 1.6):
                                    stop_hit = False
                            tp_hit = ret >= dyn_tp
                            # trail: 수수료 이상 수익 구간에서만 작동 (수수료 이하에서 trail 탈출 방지)
                            trail_hit = be_lock and trail_ret <= -max(0.003, dyn_sl * 0.6) and ret > round_trip_fee

                            if stop_hit or tp_hit or trail_hit or stagnation_hit or timeout_hit:
                                close_side = 'SELL' if pos == 'LONG' else 'BUY'
                                order = place_market_order(base_url, key, secret, sym, close_side, qty, reduce_only=True, real_order=real_order)
                                reason = (
                                    'TP' if tp_hit else
                                    'TRAIL' if trail_hit else
                                    'SL' if stop_hit else
                                    'STAGNATION' if stagnation_hit else
                                    'TIMEOUT'
                                )
                                append(f"EXIT {sym} {pos} reason={reason} ret={ret:.4f} qty={qty} orderId={order.get('orderId')}")
                                trade_strategy_name = st[sym].get('strategy_name', strategy_meta['name'])
                                trade_strategy_version = st[sym].get('strategy_version', strategy_meta['version'])
                                log_trade({
                                    'ts': now(),
                                    'symbol': sym,
                                    'iv': iv,
                                    'type': 'EXIT',
                                    'side': pos,
                                    'reason': reason,
                                    'ret': round(ret, 5),
                                    'qty': qty,
                                    'orderId': order.get('orderId'),
                                    'strategy': trade_strategy_name,
                                    'strategy_version': trade_strategy_version,
                                })
                                # 텔레그램 알림
                                try:
                                    notify_close(sym, pos.lower(), entry, px, ret * 100, reason, dry=not real_order)
                                except Exception:
                                    pass
                                st[sym] = {
                                    'position': 'FLAT',
                                    'last_key': st[sym].get('last_key', ''),
                                    'last_exit_ts': int(time.time()),
                                }
                                open_count = max(0, open_count - 1)
                                # 연속 SL 카운터 갱신
                                if reason == 'SL':
                                    consecutive_losses += 1
                                    if consecutive_losses >= max_consecutive_losses_cfg:
                                        cooldown_until = int(time.time()) + cooldown_minutes * 60
                                        append(f"COOLDOWN consecutive_losses={consecutive_losses} pausing {cooldown_minutes}min until={cooldown_until}")
                                        consecutive_losses = 0
                                else:
                                    consecutive_losses = 0

                                # 손실 종료 심볼 재진입 쿨다운
                                if reason in ('SL', 'STAGNATION'):
                                    symbol_cooldown_until[sym] = int(time.time()) + (loss_symbol_cooldown_minutes * 60)
                                continue

                    keyc = f"{iv}:{int(df.iloc[-1]['open_time'])}"
                    if st[sym].get('last_key') == keyc:
                        continue

                    sig = signal(
                        df,
                        int(cfg.get('breakout_lookback', 20)),
                        float(cfg.get('volume_spike_mult', 2.5)),
                        strategy=strategy_cfg,
                    )
                    if st[sym].get('position') == 'FLAT' and sig in ('LONG', 'SHORT') and open_count < max_positions:
                        if trading_halted:
                            st[sym]['last_key'] = keyc
                            continue
                        # ─── 드로다운 보호: 일일 손실 한도 초과 시 진입 차단 ───
                        daily_max_loss = float(cfg.get('daily_max_loss_usdt', 15.0))
                        today_str = datetime.now(timezone(timedelta(hours=9))).strftime('%Y-%m-%d')
                        today_pnl = 0.0
                        try:
                            if TRADES.exists():
                                for _line in TRADES.read_text().strip().split('\n')[-200:]:
                                    _t = json.loads(_line)
                                    if _t.get('type') == 'EXIT' and _t.get('ts', '')[:10] == today_str:
                                        today_pnl += float(_t.get('ret', 0)) * 40 * 3
                        except Exception:
                            pass
                        if today_pnl < -daily_max_loss:
                            if not getattr(scan_once, '_dd_warned', False):
                                append(f"DRAWDOWN_HALT daily_pnl={today_pnl:.2f}$ limit=-{daily_max_loss}$")
                                scan_once._dd_warned = True
                            st[sym]['last_key'] = keyc
                            continue
                        else:
                            scan_once._dd_warned = False
                        # 연속 손실 쿨다운 중이면 진입 금지
                        if int(time.time()) < cooldown_until:
                            st[sym]['last_key'] = keyc
                            continue
                        # 글로벌 진입 간격 쿨다운 (버스트 방지)
                        if (int(time.time()) - last_global_entry_ts) < global_entry_cooldown_sec:
                            st[sym]['last_key'] = keyc
                            continue
                        # 손실 다발 시간대(KST) 진입 제한
                        hour_kst = datetime.now(timezone(timedelta(hours=9))).hour
                        if blocked_hours_kst and hour_kst in blocked_hours_kst:
                            st[sym]['last_key'] = keyc
                            continue

                        # 세력 행동 추적 필터(체결강도/OI/스푸핑)
                        exec_ratio = execution_strength_ratio(df, lookback=12)
                        if enable_execution_strength_filter:
                            if sig == 'LONG' and exec_ratio < min_exec_strength_ratio:
                                st[sym]['last_key'] = keyc
                                continue
                            if sig == 'SHORT' and (1.0 / max(exec_ratio, 1e-9)) < min_exec_strength_ratio:
                                st[sym]['last_key'] = keyc
                                continue

                        if enable_open_interest_filter:
                            oi_now = open_interest(base_url, sym)
                            oi_prev = float(st[sym].get('prev_oi', oi_now) or oi_now)
                            oi_ratio = (oi_now / max(oi_prev, 1e-9)) if oi_prev > 0 else 1.0
                            st[sym]['prev_oi'] = oi_now
                            # OI 급증 + 신호 초기 구간은 함정 가능성으로 스킵
                            if oi_ratio > max_oi_spike_ratio:
                                st[sym]['last_key'] = keyc
                                continue

                        if enable_spoofing_filter:
                            wall_ratio = depth_wall_ratio(base_url, sym, limit=20)
                            prev_wall = float(st[sym].get('prev_wall_ratio', wall_ratio) or wall_ratio)
                            st[sym]['prev_wall_ratio'] = wall_ratio
                            # 비정상 벽 급출현/급소멸 구간은 스킵
                            if wall_ratio > spoof_wall_ratio_threshold or (prev_wall > spoof_wall_ratio_threshold and wall_ratio < (spoof_wall_ratio_threshold * 0.5)):
                                st[sym]['last_key'] = keyc
                                continue

                        htf_df = klines(base_url, sym, htf_interval, limit=120)
                        if not htf_trend_ok(htf_df, sig):
                            st[sym]['last_key'] = keyc
                            continue
                        if enable_otz_filter and not otz_ok(htf_df, sig):
                            st[sym]['last_key'] = keyc
                            continue
                        if enable_choch_bos_filter and not choch_bos_ok(df, sig, int(cfg.get('breakout_lookback', 20))):
                            st[sym]['last_key'] = keyc
                            continue
                        if enable_unicorn_filter and not unicorn_overlap_ok(df, sig):
                            st[sym]['last_key'] = keyc
                            continue
                        if enable_ote_filter and not ote_fvg_ok(df, sig):
                            st[sym]['last_key'] = keyc
                            continue
                        # 진입 품질 필터
                        recent_vol_ma = float(df['volume'].tail(20).mean() or 0)
                        cur_vol = float(df.iloc[-1]['volume'] or 0)
                        vol_mult = (cur_vol / recent_vol_ma) if recent_vol_ma > 0 else 0
                        h = float(df.iloc[-1]['high']); l = float(df.iloc[-1]['low'])
                        o = float(df.iloc[-1]['open']); c = float(df.iloc[-1]['close'])
                        body_ratio = abs(c - o) / max(h - l, 1e-9)

                        # QM 패턴이면 진입 품질 문턱을 소폭 완화(보너스)
                        qm_ok = enable_qm_bonus and qm_bonus_ok(df, sig)
                        req_vol = entry_min_volume_mult - (0.2 if qm_ok else 0.0)
                        req_body = entry_min_body_ratio - (0.03 if qm_ok else 0.0)
                        if vol_mult < max(req_vol, 1.2) or body_ratio < max(req_body, 0.12):
                            st[sym]['last_key'] = keyc
                            continue

                        # 동일 심볼 재진입 쿨다운
                        last_exit_ts = int(st[sym].get('last_exit_ts', 0) or 0)
                        if last_exit_ts > 0 and (int(time.time()) - last_exit_ts) < reentry_cooldown_sec:
                            st[sym]['last_key'] = keyc
                            continue

                        # ── Execution spread gate ───────────────────────────
                        sratio = spread_ratio(base_url, sym)
                        if max_spread_ratio > 0 and sratio > max_spread_ratio:
                            st[sym]['last_key'] = keyc
                            continue

                        # ── Layer 2: AI Signal Confirmation ────────────────
                        ai_confidence = 0.0
                        ai_direction = ''
                        ai_vote = 0
                        if _ai_manager is not None:
                            try:
                                closes = [float(r['close']) for _, r in df.iterrows()]
                                ohlcv = df[['open', 'high', 'low', 'close', 'volume']].values.astype(float) if len(df) >= 64 else None
                                blended = _ai_manager.generate_signal(closes, ohlcv, int(time.time() * 1000))
                                if blended is not None:
                                    ai_confidence = blended.confidence
                                    ai_direction = blended.direction
                                    min_conf = float(ai_cfg.get('min_ai_confidence', 0.55))
                                    block_mode = ai_cfg.get('ai_block_mode', 'soft')

                                    if ai_confidence >= min_conf and ai_direction:
                                        if ai_direction == 'LONG':
                                            ai_sig = 'LONG'
                                        elif ai_direction == 'SHORT':
                                            ai_sig = 'SHORT'
                                        else:
                                            ai_sig = sig  # NEUTRAL → 기존 시그널 유지

                                        if ai_sig != sig:
                                            if block_mode == 'hard':
                                                # AI 방향으로 시그널 오버라이드
                                                append(f"AI_OVERRIDE {sym} heuristic={sig} → ai={ai_sig} conf={ai_confidence:.3f}")
                                                sig = ai_sig
                                            else:
                                                append(f"AI_WARN {sym} sig={sig} ai={ai_direction} conf={ai_confidence:.3f} (soft-pass)")
                                        else:
                                            ai_vote = 1
                                            append(f"AI_AGREE {sym} sig={sig} conf={ai_confidence:.3f}")
                            except Exception as e:
                                append(f"AI_SIGNAL_ERROR {sym}: {e}")

                        # ── Entry vote gate (2-of-N) ───────────────────────
                        votes = 1 + ai_vote + (1 if qm_ok else 0) + (1 if exec_ratio >= (min_exec_strength_ratio + 0.03) else 0)
                        if votes < max(min_entry_votes, 1):
                            st[sym]['last_key'] = keyc
                            continue

                        # ── 즉사 SL 방지: 즉시 진입 → 2-pass 확인 대기 ────────────────
                        pending_signals[sym] = {
                            'sig': sig,
                            'ts': time.time(),
                            'trigger_px': px,
                            'iv': iv,
                            'vol_mult': round(vol_mult, 3),
                            'body_ratio': round(body_ratio, 3),
                            'ai_confidence': round(ai_confidence, 3),
                            'ai_direction': ai_direction,
                        }
                        append(
                            f"PENDING_ENTRY {sym} {sig} iv={iv} "
                            f"vm={vol_mult:.2f} br={body_ratio:.2f} qm={int(qm_ok)} votes={votes} spr={sratio:.4f} px={px:.6g}"
                        )

                    st[sym]['last_key'] = keyc

            save_state(st)
            time.sleep(max(loop_sleep_sec, 0.1))
        except Exception as e:
            append(f"ERROR {type(e).__name__}: {e}")
            time.sleep(3)


if __name__ == '__main__':
    main()
