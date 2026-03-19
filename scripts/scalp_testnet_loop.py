#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests
import yaml

BASE = Path('/Users/riot91naver.com/Desktop/2026/volky-bot')
sys.path.insert(0, str(BASE))
from strategies.scalp_breakout import load_strategy, signal, strategy_name, strategy_version

CFG = BASE / 'config' / 'scalping.yaml'
STRATEGY_CFG = BASE / 'config' / 'scalp_strategy.yaml'
STATE = BASE / 'papertrade' / 'scalp_state.json'
TRADES = BASE / 'papertrade' / 'scalp_trades.jsonl'
LOG = BASE / 'papertrade' / 'scalp_session.log'


def now():
    return datetime.now(timezone.utc).isoformat()


def append(msg: str):
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open('a', encoding='utf-8') as f:
        f.write(f'[{now()}] {msg}\n')


def log_trade(ev: dict):
    with TRADES.open('a', encoding='utf-8') as f:
        f.write(json.dumps(ev, ensure_ascii=False) + '\n')


def load_cfg():
    return yaml.safe_load(CFG.read_text(encoding='utf-8'))


def load_state(symbols):
    if not STATE.exists():
        d = {s: {"equity": 300.0, "position": "FLAT", "entry": 0.0, "qty": 0.0, "last_key": ""} for s in symbols}
        STATE.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding='utf-8')
    return json.loads(STATE.read_text(encoding='utf-8'))


def save_state(st):
    STATE.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding='utf-8')


def klines(base_url, symbol, interval, limit=120):
    url = f"{base_url}/fapi/v1/klines"
    r = requests.get(url, params={"symbol": symbol, "interval": interval, "limit": limit}, timeout=10)
    r.raise_for_status()
    raw = r.json()
    df = pd.DataFrame(raw, columns=['open_time', 'open', 'high', 'low', 'close', 'volume', 'close_time', 'qav', 'n', 'tb', 'tq', 'ig'])
    for c in ['open', 'high', 'low', 'close', 'volume']:
        df[c] = pd.to_numeric(df[c], errors='coerce')
    return df


def main():
    cfg = load_cfg()
    cfg_mtime = CFG.stat().st_mtime if CFG.exists() else 0.0
    strategy_cfg = load_strategy(STRATEGY_CFG)
    strategy_mtime = STRATEGY_CFG.stat().st_mtime if STRATEGY_CFG.exists() else 0.0
    strategy_meta = {"name": strategy_name(strategy_cfg), "version": strategy_version(strategy_cfg)}
    symbols = cfg['symbols']
    intervals = cfg['intervals']
    base_url = cfg.get('base_url', 'https://testnet.binancefuture.com')
    st = load_state(symbols)

    append(f"start scalping testnet symbols={symbols} intervals={intervals}")

    while True:
        try:
            current_cfg_mtime = CFG.stat().st_mtime if CFG.exists() else 0.0
            if current_cfg_mtime and current_cfg_mtime != cfg_mtime:
                cfg = load_cfg()
                cfg_mtime = current_cfg_mtime
                symbols = cfg['symbols']
                intervals = cfg['intervals']
                for symbol in symbols:
                    st.setdefault(symbol, {"equity": 300.0, "position": "FLAT", "entry": 0.0, "qty": 0.0, "last_key": ""})
                append("CONFIG_RELOADED")

            current_strategy_mtime = STRATEGY_CFG.stat().st_mtime if STRATEGY_CFG.exists() else 0.0
            if current_strategy_mtime != strategy_mtime:
                strategy_cfg = load_strategy(STRATEGY_CFG)
                strategy_mtime = current_strategy_mtime
                strategy_meta = {"name": strategy_name(strategy_cfg), "version": strategy_version(strategy_cfg)}
                append(f"STRATEGY_RELOADED name={strategy_meta['name']} version={strategy_meta['version']}")

            for sym in symbols:
                for iv in intervals:
                    df = klines(base_url, sym, iv)
                    key = f"{iv}:{int(df.iloc[-1]['open_time'])}"
                    s = st[sym]
                    if s['last_key'] == key:
                        continue

                    price = float(df.iloc[-1]['close'])
                    sig = signal(
                        df,
                        cfg.get('breakout_lookback', 20),
                        cfg.get('volume_spike_mult', 2.5),
                        strategy=strategy_cfg,
                    )

                    if s['position'] == 'FLAT' and sig in ('LONG', 'SHORT'):
                        risk = cfg.get('risk_per_trade_pct', 0.8) / 100.0
                        sl_pct = cfg.get('sl_pct', 0.006)
                        risk_dollar = s['equity'] * risk
                        qty = risk_dollar / max(price * sl_pct, 1e-9)
                        s.update({'position': sig, 'entry': price, 'qty': qty, 'strategy_name': strategy_meta['name'], 'strategy_version': strategy_meta['version']})
                        append(f"ENTRY {sym} {sig} iv={iv} price={price:.4f} qty={qty:.6f}")
                        log_trade({'ts': now(), 'symbol': sym, 'iv': iv, 'type': 'ENTRY', 'side': sig, 'price': price, 'qty': round(qty, 6), 'strategy': strategy_meta['name'], 'strategy_version': strategy_meta['version']})

                    elif s['position'] in ('LONG', 'SHORT'):
                        direction = 1 if s['position'] == 'LONG' else -1
                        ret = (price - s['entry']) / s['entry'] * direction
                        tp2 = cfg.get('tp2_pct', 0.016)
                        sl = cfg.get('sl_pct', 0.006)
                        if ret >= tp2 or ret <= -sl:
                            pnl = s['qty'] * (price - s['entry']) * direction
                            s['equity'] += pnl
                            append(f"EXIT {sym} {s['position']} iv={iv} ret={ret:.4f} pnl={pnl:.2f} eq={s['equity']:.2f}")
                            log_trade({'ts': now(), 'symbol': sym, 'iv': iv, 'type': 'EXIT', 'side': s['position'], 'entry': s['entry'], 'exit': price, 'pnl': round(pnl, 2), 'equity': round(s['equity'], 2), 'strategy': s.get('strategy_name', strategy_meta['name']), 'strategy_version': s.get('strategy_version', strategy_meta['version'])})
                            s.update({'position': 'FLAT', 'entry': 0.0, 'qty': 0.0})

                    s['last_key'] = key
                    st[sym] = s

            save_state(st)
            time.sleep(8)
        except Exception as e:
            append(f"ERROR {type(e).__name__}: {e}")
            time.sleep(8)


if __name__ == '__main__':
    main()
