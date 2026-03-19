"""
volky-bot / engine / backtester.py
전략 백테스트 — Binance 선물 과거 데이터 사용
딥러닝 없이 순수 룰 기반 시뮬레이션
"""

import requests
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

BINANCE_FAPI = "https://fapi.binance.com"

def fetch_historical_klines(
    symbol: str,
    interval: str,
    days: int = 30,
) -> list:
    end_ms   = int(time.time() * 1000)
    start_ms = end_ms - days * 24 * 3600 * 1000
    klines   = []
    limit    = 1500

    while start_ms < end_ms:
        url = f"{BINANCE_FAPI}/fapi/v1/klines"
        params = {
            "symbol":    symbol,
            "interval":  interval,
            "startTime": start_ms,
            "endTime":   end_ms,
            "limit":     limit,
        }
        try:
            res = requests.get(url, params=params, timeout=10)
            batch = res.json()
            if not batch:
                break
            klines.extend(batch)
            start_ms = int(batch[-1][0]) + 1
        except Exception as e:
            print(f"[BT] 캔들 조회 오류: {e}")
            break

    return klines


def run_backtest(
    strategy: dict,
    symbol:    str = "BTCUSDT",
    timeframe: str = "5m",
    days:      int = 30,
    leverage:  int = 3,
) -> Optional[dict]:
    """
    전략 백테스트 실행
    딥러닝 없이 entry_conditions 룰 기반으로 시뮬레이션
    """
    print(f"[BT] {strategy.get('name')} | {symbol} {timeframe} {days}일")

    klines = fetch_historical_klines(symbol, timeframe, days)
    if len(klines) < 50:
        print(f"[BT] 데이터 부족: {len(klines)}개")
        return None

    # OHLCV 파싱
    candles = [
        {
            "time":   int(k[0]),
            "open":   float(k[1]),
            "high":   float(k[2]),
            "low":    float(k[3]),
            "close":  float(k[4]),
            "volume": float(k[5]),
        }
        for k in klines
    ]

    # 지표 계산
    candles = _add_indicators(candles)

    # 거래 시뮬레이션
    trades      = []
    position    = None
    equity      = 1000.0
    peak_equity = 1000.0
    max_dd      = 0.0

    rr_min = strategy.get("risk_reward_min", 1.5)
    sl_pct = 0.004   # 기본 SL 0.4%
    tp_pct = sl_pct * rr_min

    for i in range(20, len(candles)):
        c = candles[i]
        prev = candles[i-1]

        # ── 포지션 청산 체크 ──────────────────────────
        if position:
            hit_sl = (
                (position["side"] == "long"  and c["low"]  <= position["sl"]) or
                (position["side"] == "short" and c["high"] >= position["sl"])
            )
            hit_tp = (
                (position["side"] == "long"  and c["high"] >= position["tp"]) or
                (position["side"] == "short" and c["low"]  <= position["tp"])
            )

            if hit_tp or hit_sl:
                pnl_pct = tp_pct if hit_tp else -sl_pct
                pnl_pct *= leverage
                equity  *= (1 + pnl_pct)

                trades.append({
                    "entry": position["entry"],
                    "exit":  c["close"],
                    "side":  position["side"],
                    "win":   hit_tp,
                    "pnl":   round(pnl_pct * 100, 3),
                })

                peak_equity = max(peak_equity, equity)
                dd = (peak_equity - equity) / peak_equity
                max_dd = max(max_dd, dd)
                position = None

        # ── 진입 신호 체크 ────────────────────────────
        if not position:
            signal = _check_signal(candles, i, strategy)
            if signal in ("long", "short"):
                entry = c["close"]
                if signal == "long":
                    sl = entry * (1 - sl_pct)
                    tp = entry * (1 + tp_pct)
                else:
                    sl = entry * (1 + sl_pct)
                    tp = entry * (1 - tp_pct)
                position = {"side": signal, "entry": entry, "sl": sl, "tp": tp}

    # ── 통계 계산 ─────────────────────────────────────
    if len(trades) < 3:
        print(f"[BT] 거래 부족: {len(trades)}건")
        return None

    wins     = [t for t in trades if t["win"]]
    losses   = [t for t in trades if not t["win"]]
    win_rate = len(wins) / len(trades)
    avg_win  = sum(t["pnl"] for t in wins) / max(len(wins), 1)
    avg_loss = sum(t["pnl"] for t in losses) / max(len(losses), 1)
    total_pnl= sum(t["pnl"] for t in trades)

    # Sharpe (간략 계산)
    pnls     = [t["pnl"] for t in trades]
    avg_pnl  = total_pnl / len(pnls)
    std_pnl  = (sum((p - avg_pnl)**2 for p in pnls) / len(pnls)) ** 0.5
    sharpe   = round(avg_pnl / max(std_pnl, 0.001) * (252**0.5), 3)

    result = {
        "symbol":       symbol,
        "timeframe":    timeframe,
        "days":         days,
        "total_trades": len(trades),
        "win_rate":     round(win_rate, 3),
        "avg_win_pct":  round(avg_win, 3),
        "avg_loss_pct": round(avg_loss, 3),
        "total_pnl":    round(total_pnl, 2),
        "max_drawdown": round(max_dd, 4),
        "sharpe":       sharpe,
        "final_equity": round(equity, 2),
    }

    print(
        f"[BT] 완료 — 거래={len(trades)}  "
        f"승률={win_rate:.0%}  "
        f"Sharpe={sharpe:.2f}  "
        f"MDD={max_dd:.1%}"
    )
    return result


def _add_indicators(candles: list) -> list:
    """EMA, ATR, 거래량 배수 계산"""
    closes  = [c["close"]  for c in candles]
    highs   = [c["high"]   for c in candles]
    lows    = [c["low"]    for c in candles]
    volumes = [c["volume"] for c in candles]

    # EMA
    def ema(data, period):
        k = 2 / (period + 1)
        result = [data[0]]
        for v in data[1:]:
            result.append(v * k + result[-1] * (1 - k))
        return result

    ema13  = ema(closes, 13)
    ema50  = ema(closes, 50)
    ema200 = ema(closes, 200)

    # ATR
    atrs = [highs[0] - lows[0]]
    for i in range(1, len(candles)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i-1]),
            abs(lows[i]  - closes[i-1])
        )
        atrs.append(tr * (1/14) + atrs[-1] * (13/14))

    # 거래량 배수 (20봉 평균 대비)
    vol_mults = [1.0] * 20
    for i in range(20, len(candles)):
        avg_vol = sum(volumes[i-20:i]) / 20
        vol_mults.append(volumes[i] / max(avg_vol, 1))

    for i, c in enumerate(candles):
        c["ema13"]    = ema13[i]
        c["ema50"]    = ema50[i]
        c["ema200"]   = ema200[i]
        c["atr"]      = atrs[i]
        c["vol_mult"] = vol_mults[i]

    return candles


def _check_signal(candles: list, i: int, strategy: dict) -> str:
    """
    전략 conditions를 룰 기반으로 체크
    단순화된 시뮬레이션 — 실제 신호 판단은 LLM이 담당
    """
    c    = candles[i]
    prev = candles[i-1]

    direction = strategy.get("entry", {}).get("direction", "both")
    bias      = strategy.get("market_bias", "trend_following")

    # 거래량 필터 (공통)
    if c["vol_mult"] < 1.5:
        return "none"

    # 추세추종
    if bias == "trend_following":
        if direction in ("long", "both"):
            if (c["close"] > c["ema13"] > c["ema50"] and
                c["close"] > prev["close"] and
                c["vol_mult"] >= 2.0):
                return "long"
        if direction in ("short", "both"):
            if (c["close"] < c["ema13"] < c["ema50"] and
                c["close"] < prev["close"] and
                c["vol_mult"] >= 2.0):
                return "short"

    # 평균회귀
    elif bias == "mean_reversion":
        atr = c["atr"]
        if direction in ("long", "both"):
            if (c["low"] < c["ema50"] - atr and
                c["close"] > c["open"] and
                c["vol_mult"] >= 1.8):
                return "long"
        if direction in ("short", "both"):
            if (c["high"] > c["ema50"] + atr and
                c["close"] < c["open"] and
                c["vol_mult"] >= 1.8):
                return "short"

    # 돌파
    elif bias == "breakout":
        recent_high = max(candles[i-10:i], key=lambda x: x["high"])["high"]
        recent_low  = min(candles[i-10:i], key=lambda x: x["low"])["low"]
        if direction in ("long", "both"):
            if c["close"] > recent_high * 1.001 and c["vol_mult"] >= 2.5:
                return "long"
        if direction in ("short", "both"):
            if c["close"] < recent_low * 0.999 and c["vol_mult"] >= 2.5:
                return "short"

    return "none"
