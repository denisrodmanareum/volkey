from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Optional

import pandas as pd

BASE = Path(__file__).resolve().parent.parent
DEFAULT_STRATEGY_PATH = BASE / "config" / "scalp_strategy.yaml"


def _nested_get(data: dict[str, Any], keys: list[str], default: Any) -> Any:
    cur: Any = data
    for key in keys:
        if not isinstance(cur, dict) or key not in cur:
            return default
        cur = cur[key]
    return cur


def _safe_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _safe_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except Exception:
        return default


def load_strategy(path: Optional[Path] = None) -> dict[str, Any]:
    target = path or DEFAULT_STRATEGY_PATH
    if not target.exists():
        return {}
    try:
        import yaml

        raw = yaml.safe_load(target.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


def strategy_name(strategy: Optional[dict[str, Any]]) -> str:
    if isinstance(strategy, dict):
        return str(strategy.get("name") or "scalp_breakout")
    return "scalp_breakout"


def strategy_version(strategy: Optional[dict[str, Any]]) -> str:
    payload = strategy if isinstance(strategy, dict) else {}
    try:
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except Exception:
        raw = "{}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def _rsi(series: pd.Series, period: int) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / max(period, 1), adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / max(period, 1), adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, pd.NA)
    rsi = 100 - (100 / (1 + rs))
    return rsi.fillna(50.0)


def _macd(series: pd.Series, fast_period: int, slow_period: int, signal_period: int) -> tuple[pd.Series, pd.Series, pd.Series]:
    fast = series.ewm(span=max(fast_period, 1), adjust=False).mean()
    slow = series.ewm(span=max(slow_period, 1), adjust=False).mean()
    macd_line = fast - slow
    signal_line = macd_line.ewm(span=max(signal_period, 1), adjust=False).mean()
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


def _vwap(df: pd.DataFrame) -> pd.Series:
    typical = (df["high"] + df["low"] + df["close"]) / 3.0
    vol = df["volume"].replace(0, pd.NA)
    return (typical.mul(vol).cumsum() / vol.cumsum()).fillna(df["close"])


def _body_ratio(cur: pd.Series) -> float:
    return abs(float(cur["close"]) - float(cur["open"])) / max(float(cur["high"]) - float(cur["low"]), 1e-9)


def _trigger_bar(d: pd.DataFrame, use_live_bar: bool) -> pd.Series:
    return d.iloc[-1] if use_live_bar else d.iloc[-2]


def _history_window(d: pd.DataFrame, lookback: int, use_live_bar: bool) -> pd.DataFrame:
    if use_live_bar:
        return d.iloc[-(lookback + 1):-1]
    return d.iloc[-(lookback + 2):-2]


def _long_breakout_ok(d: pd.DataFrame, lookback: int, source: str, use_live_bar: bool = False) -> bool:
    prev = _trigger_bar(d, use_live_bar)
    history = _history_window(d, lookback, use_live_bar)
    ref = history["high"].max() if source == "high" else history["close"].max()
    return float(prev["close"]) > float(ref)


def _short_breakout_ok(d: pd.DataFrame, lookback: int, source: str, use_live_bar: bool = False) -> bool:
    prev = _trigger_bar(d, use_live_bar)
    history = _history_window(d, lookback, use_live_bar)
    ref = history["low"].min() if source == "low" else history["close"].min()
    return float(prev["close"]) < float(ref)


def _price_structure_ok(d: pd.DataFrame, side: str, strategy: dict[str, Any]) -> bool:
    cfg = _nested_get(strategy, ["filters", "price_structure"], {})
    if not cfg or not cfg.get("enabled", False):
        return True
    swing_lookback = _safe_int(cfg.get("swing_lookback", 8), 8)
    if len(d) < swing_lookback + 4:
        return False
    prev = d.iloc[-2]
    recent = d.iloc[-(swing_lookback + 2):-2]
    if side == "LONG":
        if float(prev["low"]) <= float(recent["low"].min()):
            return False
        if cfg.get("require_long_higher_low", False):
            first_half = recent.iloc[: max(len(recent) // 2, 1)]["low"].min()
            second_half = recent.iloc[max(len(recent) // 2, 1):]["low"].min()
            return float(second_half) >= float(first_half)
        return True
    if float(prev["high"]) >= float(recent["high"].max()):
        return False
    if cfg.get("require_short_lower_high", False):
        first_half = recent.iloc[: max(len(recent) // 2, 1)]["high"].max()
        second_half = recent.iloc[max(len(recent) // 2, 1):]["high"].max()
        return float(second_half) <= float(first_half)
    return True


def _filter_ok(side: str, d: pd.DataFrame, strategy: dict[str, Any]) -> bool:
    close = d["close"]

    ema_cfg = _nested_get(strategy, ["filters", "ema_trend"], {})
    if ema_cfg.get("enabled", False):
        fast_period = _safe_int(ema_cfg.get("fast_period", 20), 20)
        slow_period = _safe_int(ema_cfg.get("slow_period", 50), 50)
        ema_fast = close.ewm(span=max(fast_period, 1), adjust=False).mean().iloc[-2]
        ema_slow = close.ewm(span=max(slow_period, 1), adjust=False).mean().iloc[-2]
        if side == "LONG" and ema_fast < ema_slow:
            return False
        if side == "SHORT" and ema_fast > ema_slow:
            return False

    rsi_cfg = _nested_get(strategy, ["filters", "rsi"], {})
    if rsi_cfg.get("enabled", False):
        rsi_period = _safe_int(rsi_cfg.get("period", 14), 14)
        rsi_val = float(_rsi(close, rsi_period).iloc[-2])
        if side == "LONG" and rsi_val < _safe_float(rsi_cfg.get("long_min", 52), 52):
            return False
        if side == "SHORT" and rsi_val > _safe_float(rsi_cfg.get("short_max", 48), 48):
            return False

    macd_cfg = _nested_get(strategy, ["filters", "macd"], {})
    if macd_cfg.get("enabled", False):
        macd_line, signal_line, hist = _macd(
            close,
            _safe_int(macd_cfg.get("fast_period", 12), 12),
            _safe_int(macd_cfg.get("slow_period", 26), 26),
            _safe_int(macd_cfg.get("signal_period", 9), 9),
        )
        macd_prev = float(macd_line.iloc[-2])
        signal_prev = float(signal_line.iloc[-2])
        hist_prev = float(hist.iloc[-2])
        if side == "LONG" and macd_prev < signal_prev:
            return False
        if side == "SHORT" and macd_prev > signal_prev:
            return False
        if macd_cfg.get("require_histogram_confirmation", False):
            if side == "LONG" and hist_prev <= 0:
                return False
            if side == "SHORT" and hist_prev >= 0:
                return False

    vwap_cfg = _nested_get(strategy, ["filters", "vwap"], {})
    if vwap_cfg.get("enabled", False):
        vwap_prev = float(_vwap(d).iloc[-2])
        close_prev = float(d.iloc[-2]["close"])
        if side == "LONG" and vwap_cfg.get("long_above", True) and close_prev < vwap_prev:
            return False
        if side == "SHORT" and vwap_cfg.get("short_below", True) and close_prev > vwap_prev:
            return False

    return _price_structure_ok(d, side, strategy)


def build_legacy_strategy(lookback: int = 20, vol_mult: float = 2.5) -> dict[str, Any]:
    return {
        "enabled": True,
        "min_candles": max(lookback + 5, 30),
        "entry": {
            "breakout": {
                "enabled": True,
                "long_lookback": lookback,
                "short_lookback": lookback,
                "source": "close",
            },
            "volume": {
                "enabled": True,
                "lookback": lookback,
                "spike_mult": vol_mult,
            },
            "candle": {
                "enabled": False,
                "min_body_ratio": 0.0,
            },
        },
        "filters": {},
    }


def signal(
    df: pd.DataFrame,
    lookback: int = 20,
    vol_mult: float = 2.5,
    strategy: Optional[dict[str, Any]] = None,
) -> str:
    cfg = strategy or build_legacy_strategy(lookback=lookback, vol_mult=vol_mult)
    min_candles = _safe_int(cfg.get("min_candles", max(lookback + 2, 30)), max(lookback + 2, 30))
    if len(df) < min_candles:
        return "FLAT"

    d = df.copy()
    volume_cfg = _nested_get(cfg, ["entry", "volume"], {})
    volume_lookback = _safe_int(volume_cfg.get("lookback", lookback), lookback)
    spike_mult = _safe_float(volume_cfg.get("spike_mult", vol_mult), vol_mult)
    d["vol_ma"] = d["volume"].rolling(volume_lookback).mean()
    prev = d.iloc[-2]

    breakout_cfg = _nested_get(cfg, ["entry", "breakout"], {})
    long_lookback = _safe_int(breakout_cfg.get("long_lookback", lookback), lookback)
    short_lookback = _safe_int(breakout_cfg.get("short_lookback", lookback), lookback)
    source = str(breakout_cfg.get("source", "close")).lower()
    use_live_bar = bool(breakout_cfg.get("use_live_bar", False))

    volume_ok = True
    trigger_bar = _trigger_bar(d, use_live_bar)
    if volume_cfg.get("enabled", True):
        volume_ok = float(trigger_bar["volume"]) >= float(trigger_bar["vol_ma"] * spike_mult if pd.notna(trigger_bar["vol_ma"]) else 0)

    candle_cfg = _nested_get(cfg, ["entry", "candle"], {})
    candle_ok = True
    if candle_cfg.get("enabled", False):
        candle_ok = _body_ratio(trigger_bar) >= _safe_float(candle_cfg.get("min_body_ratio", 0.2), 0.2)

    long_ok = volume_ok and candle_ok
    short_ok = volume_ok and candle_ok
    if breakout_cfg.get("enabled", True):
        long_ok = long_ok and _long_breakout_ok(d, long_lookback, source, use_live_bar=use_live_bar)
        short_ok = short_ok and _short_breakout_ok(d, short_lookback, source, use_live_bar=use_live_bar)

    if long_ok and _filter_ok("LONG", d, cfg):
        return "LONG"
    if short_ok and _filter_ok("SHORT", d, cfg):
        return "SHORT"
    return "FLAT"
