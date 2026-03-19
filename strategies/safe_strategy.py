from __future__ import annotations

import pandas as pd


def build_features(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["ema20"] = out["close"].ewm(span=20, adjust=False).mean()
    out["ema50"] = out["close"].ewm(span=50, adjust=False).mean()

    delta = out["close"].diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    rs = gain / (loss.replace(0, 1e-9))
    out["rsi14"] = 100 - (100 / (1 + rs))

    out["vol_ma20"] = out["volume"].rolling(20).mean()
    return out


def generate_signal(df: pd.DataFrame) -> str:
    """return one of: LONG, SHORT, FLAT"""
    f = build_features(df)
    if len(f) < 60:
        return "FLAT"

    prev = f.iloc[-2]
    cur = f.iloc[-1]

    long_cond = (
        cur["ema20"] > cur["ema50"]
        and 48 <= cur["rsi14"] <= 68
        and cur["close"] > prev["high"]
        and cur["volume"] >= (cur["vol_ma20"] or 0)
    )

    short_cond = (
        cur["ema20"] < cur["ema50"]
        and 32 <= cur["rsi14"] <= 52
        and cur["close"] < prev["low"]
        and cur["volume"] >= (cur["vol_ma20"] or 0)
    )

    if long_cond:
        return "LONG"
    if short_cond:
        return "SHORT"
    return "FLAT"
