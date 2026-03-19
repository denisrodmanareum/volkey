#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests
import yaml

BASE = Path('/Users/riot91naver.com/Desktop/2026/volky-bot')
sys.path.insert(0, str(BASE))
from strategies.safe_strategy import generate_signal

RISK_PATH = BASE / 'config' / 'risk.yaml'
STATE_PATH = BASE / 'papertrade' / 'state.json'
TRADE_LOG = BASE / 'papertrade' / 'trades.jsonl'
SESSION_LOG = BASE / 'papertrade' / 'session.log'


@dataclass
class SymbolState:
    equity: float = 10000.0
    position: str = 'FLAT'
    entry: float = 0.0
    qty: float = 0.0
    last_ts: int = 0


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_risk() -> dict:
    with open(RISK_PATH, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f)


def load_state(symbols: list[str]) -> dict[str, SymbolState]:
    if not STATE_PATH.exists():
        data = {s: asdict(SymbolState()) for s in symbols}
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
        return {s: SymbolState() for s in symbols}

    raw = json.loads(STATE_PATH.read_text(encoding='utf-8'))
    out = {}
    for s in symbols:
        out[s] = SymbolState(**raw.get(s, asdict(SymbolState())))
    return out


def save_state(state: dict[str, SymbolState]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    data = {k: asdict(v) for k, v in state.items()}
    STATE_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')


def fetch_klines(symbol='BTCUSDT', interval='15m', limit=200) -> pd.DataFrame:
    url = 'https://fapi.binance.com/fapi/v1/klines'
    r = requests.get(url, params={'symbol': symbol, 'interval': interval, 'limit': limit}, timeout=10)
    r.raise_for_status()
    raw = r.json()
    df = pd.DataFrame(raw, columns=[
        'open_time', 'open', 'high', 'low', 'close', 'volume',
        'close_time', 'quote_asset_volume', 'num_trades',
        'taker_buy_base', 'taker_buy_quote', 'ignore'
    ])
    for c in ['open', 'high', 'low', 'close', 'volume']:
        df[c] = pd.to_numeric(df[c], errors='coerce')
    df['open_time'] = df['open_time'].astype(int)
    return df


def log_trade(event: dict):
    TRADE_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(TRADE_LOG, 'a', encoding='utf-8') as f:
        f.write(json.dumps(event, ensure_ascii=False) + '\n')


def append_session(msg: str):
    SESSION_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(SESSION_LOG, 'a', encoding='utf-8') as f:
        f.write(f'[{now_iso()}] {msg}\n')


def position_size(equity: float, price: float, risk_pct: float, stop_pct: float = 0.005) -> float:
    risk_dollar = equity * (risk_pct / 100.0)
    qty = risk_dollar / max(price * stop_pct, 1e-9)
    return max(qty, 0.0)


def run_symbol(symbol: str, st: SymbolState, risk: dict) -> SymbolState:
    df = fetch_klines(symbol=symbol, interval='15m', limit=200)
    ts = int(df.iloc[-1]['open_time'])
    price = float(df.iloc[-1]['close'])

    if ts == st.last_ts:
        return st

    sig = generate_signal(df)

    if st.position == 'FLAT' and sig in ('LONG', 'SHORT'):
        qty = position_size(st.equity, price, float(risk.get('risk_per_trade_pct', 0.5)))
        st.position = sig
        st.entry = price
        st.qty = qty
        log_trade({'ts': now_iso(), 'symbol': symbol, 'type': 'ENTRY', 'side': sig, 'price': price, 'qty': round(qty, 6), 'equity': round(st.equity, 2)})
        append_session(f'ENTRY {symbol} {sig} price={price:.4f} qty={qty:.6f}')

    elif st.position in ('LONG', 'SHORT'):
        exit_now = (sig == 'FLAT') or (sig != st.position)
        if exit_now:
            direction = 1 if st.position == 'LONG' else -1
            pnl = (price - st.entry) * st.qty * direction
            st.equity += pnl
            log_trade({'ts': now_iso(), 'symbol': symbol, 'type': 'EXIT', 'side': st.position, 'entry': st.entry, 'exit': price, 'qty': round(st.qty, 6), 'pnl': round(pnl, 2), 'equity': round(st.equity, 2)})
            append_session(f'EXIT {symbol} {st.position} entry={st.entry:.4f} exit={price:.4f} pnl={pnl:.2f} eq={st.equity:.2f}')
            st.position, st.entry, st.qty = 'FLAT', 0.0, 0.0

    st.last_ts = ts
    return st


def main():
    risk = load_risk()
    symbols = risk.get('symbols') or [risk.get('market', 'BTCUSDT')]
    state = load_state(symbols)

    append_session(f'start loop symbols={symbols} mode={risk.get("mode", "SAFE")}')

    while True:
        try:
            for s in symbols:
                state[s] = run_symbol(s, state[s], risk)
            save_state(state)
            time.sleep(20)
        except Exception as e:
            append_session(f'ERROR {type(e).__name__}: {e}')
            time.sleep(10)


if __name__ == '__main__':
    main()
