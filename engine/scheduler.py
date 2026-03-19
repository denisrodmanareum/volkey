"""
volky-bot / engine / scheduler.py  (v2 — evolution_engine 통합)
"""
import json, time, logging, sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

import yaml
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from llm.evolution_engine import run_evolution_cycle, detect_phase
from llm.strategy_creator import evolve_strategy, validate_strategy, call_llm, safe_parse_json, load_prompt
from engine.backtester    import run_backtest
from engine.strategy_pool  import StrategyPool
from engine.telegram_bot   import notify_evolution, notify_emergency, notify_daily_report
from strategies.scalp_breakout import load_strategy, strategy_name, strategy_version

KST      = timezone(timedelta(hours=9))
DATA_DIR = Path(__file__).parent.parent / "data"
DATA_DIR.mkdir(exist_ok=True)
CONFIG_DIR = Path(__file__).parent.parent / "config"
STRATEGY_CFG = CONFIG_DIR / "scalp_strategy.yaml"
STRATEGY_BACKUP_DIR = CONFIG_DIR / "strategy_backups"
STRATEGY_STATE_FILE = DATA_DIR / "scalp_strategy_state.json"
STRATEGY_BACKUP_DIR.mkdir(exist_ok=True)

THRESHOLDS = {"consecutive_loss":3,"min_win_rate":0.40,"max_drawdown":0.05,"vol_spike_mult":4.0}
STRATEGY_EVAL = {
    "min_trades": 30,
    "promote_win_rate": 0.62,
    "min_avg_pnl": 0.0018,
    "min_total_pnl": 0.03,
    "max_drawdown": 0.03,
    "min_win_rate_delta": 0.03,
}

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(DATA_DIR/"scheduler.log",encoding="utf-8"),logging.StreamHandler()])
log  = logging.getLogger("volky.scheduler")
pool = StrategyPool(DATA_DIR / "strategy_pool.json")

TRADE_SOURCES = [
    Path(__file__).parent.parent / "papertrade" / "scalp_live_trades.jsonl",
    Path(__file__).parent.parent / "papertrade" / "scalp_trades.jsonl",
    Path(__file__).parent.parent / "papertrade" / "trades.jsonl",
]


def _normalize_trade(ev: dict) -> Optional[dict]:
    if not isinstance(ev, dict):
        return None
    if ev.get("type") != "EXIT":
        return None
    pnl_pct = ev.get("pnl_pct", ev.get("ret", 0))
    try:
        pnl_pct = float(pnl_pct or 0)
    except Exception:
        pnl_pct = 0.0
    return {
        "ts": ev.get("ts"),
        "symbol": ev.get("symbol", "UNKNOWN"),
        "strategy": ev.get("strategy", "scalp_breakout"),
        "strategy_version": ev.get("strategy_version"),
        "pattern": ev.get("pattern") or ev.get("iv") or "scalp_live",
        "pnl_pct": pnl_pct,
        "side": ev.get("side"),
        "reason": ev.get("reason"),
    }


def _now_kst_iso() -> str:
    return datetime.now(KST).isoformat()


def _load_strategy_state() -> dict:
    if not STRATEGY_STATE_FILE.exists():
        return {"active": None, "candidate": None, "history": []}
    try:
        raw = json.loads(STRATEGY_STATE_FILE.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            raw.setdefault("active", None)
            raw.setdefault("candidate", None)
            raw.setdefault("history", [])
            return raw
    except Exception:
        pass
    return {"active": None, "candidate": None, "history": []}


def _save_strategy_state(state: dict):
    STRATEGY_STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _today_kst() -> str:
    return datetime.now(KST).date().isoformat()


def _snapshot_path(name: str, version: str, tag: str) -> Path:
    safe_name = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in name)[:40] or "strategy"
    return STRATEGY_BACKUP_DIR / f"{safe_name}_{version}_{tag}.yaml"


def _write_snapshot(path: Path, text: str):
    path.write_text(text, encoding="utf-8")


def _read_text(path_str: Optional[str]) -> Optional[str]:
    if not path_str:
        return None
    path = Path(path_str)
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8")


def _trade_stats(trades: list[dict]) -> dict:
    total = len(trades)
    wins = sum(1 for t in trades if t.get("pnl_pct", 0) > 0)
    pnl_values = [float(t.get("pnl_pct", 0) or 0) for t in trades]
    total_pnl = sum(pnl_values)
    avg_pnl = total_pnl / max(total, 1)
    equity = 0.0
    peak = 0.0
    max_drawdown = 0.0
    for pnl in pnl_values:
        equity += pnl
        peak = max(peak, equity)
        drawdown = peak - equity
        max_drawdown = max(max_drawdown, drawdown)
    return {
        "total": total,
        "win_rate": wins / max(total, 1),
        "total_pnl": total_pnl,
        "avg_pnl": avg_pnl,
        "max_drawdown": max_drawdown,
    }


def _sanitize_strategy_candidate(candidate: dict, current: dict) -> dict:
    def _clamp_int(val, lo, hi, default):
        try:
            return max(lo, min(hi, int(val)))
        except Exception:
            return default

    def _clamp_float(val, lo, hi, default):
        try:
            return max(lo, min(hi, float(val)))
        except Exception:
            return default

    base = current if isinstance(current, dict) and current else {
        "name": "breakout_combo_auto",
        "enabled": True,
        "min_candles": 80,
        "entry": {"breakout": {}, "volume": {}, "candle": {}},
        "filters": {"ema_trend": {}, "rsi": {}, "macd": {}, "vwap": {}, "price_structure": {}},
    }
    out = json.loads(json.dumps(base, ensure_ascii=False))

    out["name"] = str(candidate.get("name") or f"{strategy_name(current)}_auto")
    out["enabled"] = bool(candidate.get("enabled", True))
    out["min_candles"] = _clamp_int(candidate.get("min_candles", out.get("min_candles", 80)), 40, 160, 80)

    src_breakout = ((candidate.get("entry") or {}).get("breakout") or {})
    dst_breakout = out.setdefault("entry", {}).setdefault("breakout", {})
    dst_breakout["enabled"] = bool(src_breakout.get("enabled", dst_breakout.get("enabled", True)))
    dst_breakout["long_lookback"] = _clamp_int(src_breakout.get("long_lookback", dst_breakout.get("long_lookback", 20)), 8, 60, 20)
    dst_breakout["short_lookback"] = _clamp_int(src_breakout.get("short_lookback", dst_breakout.get("short_lookback", 20)), 8, 60, 20)
    source = str(src_breakout.get("source", dst_breakout.get("source", "close"))).lower()
    dst_breakout["source"] = source if source in ("close", "high", "low") else "close"

    src_volume = ((candidate.get("entry") or {}).get("volume") or {})
    dst_volume = out["entry"].setdefault("volume", {})
    dst_volume["enabled"] = bool(src_volume.get("enabled", dst_volume.get("enabled", True)))
    dst_volume["lookback"] = _clamp_int(src_volume.get("lookback", dst_volume.get("lookback", 20)), 8, 40, 20)
    dst_volume["spike_mult"] = _clamp_float(src_volume.get("spike_mult", dst_volume.get("spike_mult", 2.2)), 1.2, 5.0, 2.2)

    src_candle = ((candidate.get("entry") or {}).get("candle") or {})
    dst_candle = out["entry"].setdefault("candle", {})
    dst_candle["enabled"] = bool(src_candle.get("enabled", dst_candle.get("enabled", True)))
    dst_candle["min_body_ratio"] = _clamp_float(src_candle.get("min_body_ratio", dst_candle.get("min_body_ratio", 0.2)), 0.05, 0.90, 0.2)

    src_filters = candidate.get("filters") or {}
    dst_filters = out.setdefault("filters", {})

    ema = src_filters.get("ema_trend") or {}
    ema_dst = dst_filters.setdefault("ema_trend", {})
    ema_dst["enabled"] = bool(ema.get("enabled", ema_dst.get("enabled", True)))
    ema_dst["fast_period"] = _clamp_int(ema.get("fast_period", ema_dst.get("fast_period", 20)), 5, 60, 20)
    ema_dst["slow_period"] = _clamp_int(ema.get("slow_period", ema_dst.get("slow_period", 50)), 10, 120, 50)
    if ema_dst["slow_period"] <= ema_dst["fast_period"]:
        ema_dst["slow_period"] = min(120, ema_dst["fast_period"] + 10)

    rsi = src_filters.get("rsi") or {}
    rsi_dst = dst_filters.setdefault("rsi", {})
    rsi_dst["enabled"] = bool(rsi.get("enabled", rsi_dst.get("enabled", True)))
    rsi_dst["period"] = _clamp_int(rsi.get("period", rsi_dst.get("period", 14)), 5, 30, 14)
    rsi_dst["long_min"] = _clamp_float(rsi.get("long_min", rsi_dst.get("long_min", 52)), 45, 70, 52)
    rsi_dst["short_max"] = _clamp_float(rsi.get("short_max", rsi_dst.get("short_max", 48)), 30, 55, 48)
    if rsi_dst["short_max"] >= rsi_dst["long_min"]:
        rsi_dst["short_max"] = max(30, rsi_dst["long_min"] - 4)

    macd = src_filters.get("macd") or {}
    macd_dst = dst_filters.setdefault("macd", {})
    macd_dst["enabled"] = bool(macd.get("enabled", macd_dst.get("enabled", False)))
    macd_dst["fast_period"] = _clamp_int(macd.get("fast_period", macd_dst.get("fast_period", 12)), 5, 20, 12)
    macd_dst["slow_period"] = _clamp_int(macd.get("slow_period", macd_dst.get("slow_period", 26)), 10, 40, 26)
    if macd_dst["slow_period"] <= macd_dst["fast_period"]:
        macd_dst["slow_period"] = min(40, macd_dst["fast_period"] + 8)
    macd_dst["signal_period"] = _clamp_int(macd.get("signal_period", macd_dst.get("signal_period", 9)), 4, 18, 9)
    macd_dst["require_histogram_confirmation"] = bool(macd.get("require_histogram_confirmation", macd_dst.get("require_histogram_confirmation", True)))

    vwap = src_filters.get("vwap") or {}
    vwap_dst = dst_filters.setdefault("vwap", {})
    vwap_dst["enabled"] = bool(vwap.get("enabled", vwap_dst.get("enabled", False)))
    vwap_dst["long_above"] = bool(vwap.get("long_above", vwap_dst.get("long_above", True)))
    vwap_dst["short_below"] = bool(vwap.get("short_below", vwap_dst.get("short_below", True)))

    ps = src_filters.get("price_structure") or {}
    ps_dst = dst_filters.setdefault("price_structure", {})
    ps_dst["enabled"] = bool(ps.get("enabled", ps_dst.get("enabled", True)))
    ps_dst["swing_lookback"] = _clamp_int(ps.get("swing_lookback", ps_dst.get("swing_lookback", 8)), 4, 30, 8)
    ps_dst["require_long_higher_low"] = bool(ps.get("require_long_higher_low", ps_dst.get("require_long_higher_low", False)))
    ps_dst["require_short_lower_high"] = bool(ps.get("require_short_lower_high", ps_dst.get("require_short_lower_high", False)))

    return out


def _heuristic_strategy_candidate(current: dict, analytics: dict) -> dict:
    out = json.loads(json.dumps(current if current else {}, ensure_ascii=False))
    recent = analytics.get("recent_trades", [])[-30:]
    recent_stats = _trade_stats(recent)
    reasons = {}
    for t in recent:
        reason = t.get("reason", "UNKNOWN")
        reasons[reason] = reasons.get(reason, 0) + 1

    # 최근 성과가 나쁘면 진입을 보수적으로, 좋으면 약간 완화
    bad = recent_stats["win_rate"] < 0.42 or recent_stats["avg_pnl"] < 0
    strong = recent_stats["win_rate"] > 0.55 and recent_stats["avg_pnl"] > 0.002

    entry = out.setdefault("entry", {})
    breakout = entry.setdefault("breakout", {})
    volume = entry.setdefault("volume", {})
    candle = entry.setdefault("candle", {})
    filters = out.setdefault("filters", {})
    ema = filters.setdefault("ema_trend", {})
    rsi = filters.setdefault("rsi", {})
    macd = filters.setdefault("macd", {})
    vwap = filters.setdefault("vwap", {})
    price_structure = filters.setdefault("price_structure", {})

    out["name"] = f"{strategy_name(current)}_{_today_kst().replace('-', '')}"

    if bad:
        breakout["long_lookback"] = int(breakout.get("long_lookback", 20)) + 4
        breakout["short_lookback"] = int(breakout.get("short_lookback", 20)) + 4
        volume["spike_mult"] = round(float(volume.get("spike_mult", 2.2)) + 0.25, 2)
        candle["min_body_ratio"] = round(float(candle.get("min_body_ratio", 0.2)) + 0.05, 2)
        ema["enabled"] = True
        ema["fast_period"] = int(ema.get("fast_period", 20))
        ema["slow_period"] = int(ema.get("slow_period", 50)) + 5
        rsi["enabled"] = True
        rsi["long_min"] = round(float(rsi.get("long_min", 52)) + 2, 1)
        rsi["short_max"] = round(float(rsi.get("short_max", 48)) - 2, 1)
        price_structure["enabled"] = True
        if reasons.get("SL", 0) >= 3:
            macd["enabled"] = True
            macd["require_histogram_confirmation"] = True
            vwap["enabled"] = True
    elif strong:
        breakout["long_lookback"] = max(8, int(breakout.get("long_lookback", 20)) - 2)
        breakout["short_lookback"] = max(8, int(breakout.get("short_lookback", 20)) - 2)
        volume["spike_mult"] = round(max(1.2, float(volume.get("spike_mult", 2.2)) - 0.1), 2)
        candle["min_body_ratio"] = round(max(0.1, float(candle.get("min_body_ratio", 0.2)) - 0.02), 2)
    else:
        # 혼합 구간이면 필터는 유지하되 약간 구조를 강화
        breakout["long_lookback"] = int(breakout.get("long_lookback", 20)) + 2
        breakout["short_lookback"] = int(breakout.get("short_lookback", 20)) + 2
        volume["spike_mult"] = round(float(volume.get("spike_mult", 2.2)) + 0.1, 2)
        price_structure["enabled"] = True
        price_structure["swing_lookback"] = int(price_structure.get("swing_lookback", 8)) + 2

    return _sanitize_strategy_candidate(out, current)


def _maybe_generate_strategy_candidate(analytics: dict, force: bool = False) -> bool:
    if not STRATEGY_CFG.exists():
        return False

    state = _load_strategy_state()
    last_generated = state.get("last_generated_date")
    today = _today_kst()
    if not force and last_generated == today:
        log.info(f"전략 후보 생성 스킵: already generated today ({today})")
        return False

    current_strategy = load_strategy(STRATEGY_CFG)
    recent = analytics.get("recent_trades", [])[-30:]
    reason_stats = {}
    for t in recent:
        reason = t.get("reason", "UNKNOWN")
        reason_stats.setdefault(reason, {"count": 0, "wins": 0, "pnl": 0.0})
        reason_stats[reason]["count"] += 1
        if t.get("pnl_pct", 0) > 0:
            reason_stats[reason]["wins"] += 1
        reason_stats[reason]["pnl"] += float(t.get("pnl_pct", 0) or 0)

    system = load_prompt("tune_scalp_strategy")
    user_message = json.dumps({
        "current_strategy": current_strategy,
        "analytics_summary": {
            "total_trades": analytics.get("total_trades", 0),
            "win_rate": analytics.get("win_rate", 0),
            "total_pnl": analytics.get("total_pnl", 0),
            "recent_trade_count": len(recent),
            "reason_stats": reason_stats,
            "recent_trades": recent[-12:],
        },
        "instruction": "현재 전략을 너무 크게 바꾸지 말고 최근 성과를 반영한 다음 후보를 생성하라.",
    }, ensure_ascii=False, indent=2)

    raw = call_llm(system, user_message, model="qwen3.5:35b", timeout=60, temperature=0.35)
    candidate = safe_parse_json(raw)
    if not candidate:
        log.warning("전략 후보 생성 실패: LLM 응답 파싱 실패, 휴리스틱 fallback 사용")
        sanitized = _heuristic_strategy_candidate(current_strategy, analytics)
    else:
        sanitized = _sanitize_strategy_candidate(candidate, current_strategy)
    current_version = strategy_version(current_strategy)
    new_version = strategy_version(sanitized)
    if new_version == current_version:
        state["last_generated_date"] = today
        state["last_generated_reason"] = "unchanged_candidate"
        _save_strategy_state(state)
        log.info("전략 후보 생성 결과가 기존과 동일하여 저장 생략")
        return False

    STRATEGY_CFG.write_text(yaml.safe_dump(sanitized, allow_unicode=True, sort_keys=False), encoding="utf-8")
    state["last_generated_date"] = today
    state["last_generated_reason"] = "llm_daily_tune" if not force else "manual_force_tune"
    state["last_generated_from_version"] = current_version
    state["last_generated_to_version"] = new_version
    _save_strategy_state(state)
    log.info(f"새 전략 후보 저장: {strategy_name(sanitized)}#{new_version} (from {current_version})")
    return True


def _baseline_stats(trades: list[dict], active_version: Optional[str]) -> dict:
    if active_version:
        active_trades = [t for t in trades if t.get("strategy_version") == active_version]
        if active_trades:
            return _trade_stats(active_trades[-20:])
    fallback = [t for t in trades if t.get("type") != "ENTRY"]
    return _trade_stats(fallback[-20:])


def _sync_strategy_candidate(trades: list[dict]):
    if not STRATEGY_CFG.exists():
        return

    state = _load_strategy_state()
    strategy_cfg = load_strategy(STRATEGY_CFG)
    current_name = strategy_name(strategy_cfg)
    current_version = strategy_version(strategy_cfg)
    current_text = STRATEGY_CFG.read_text(encoding="utf-8")

    if not state.get("active"):
        active_snapshot = _snapshot_path(current_name, current_version, "active")
        _write_snapshot(active_snapshot, current_text)
        state["active"] = {
            "name": current_name,
            "version": current_version,
            "snapshot_path": str(active_snapshot),
            "promoted_at": _now_kst_iso(),
        }
        _save_strategy_state(state)
        log.info(f"전략 상태 초기화: active={current_name}#{current_version}")
        return

    active = state["active"]
    candidate = state.get("candidate")

    if current_version == active.get("version"):
        return

    if candidate and current_version == candidate.get("version"):
        return

    candidate_snapshot = _snapshot_path(current_name, current_version, "candidate")
    _write_snapshot(candidate_snapshot, current_text)
    baseline = _baseline_stats(trades, active.get("version"))
    state["candidate"] = {
        "name": current_name,
        "version": current_version,
        "snapshot_path": str(candidate_snapshot),
        "started_at": _now_kst_iso(),
        "baseline": baseline,
        "thresholds": STRATEGY_EVAL,
        "previous_active_version": active.get("version"),
    }
    _save_strategy_state(state)
    log.info(
        "새 전략 후보 감지: "
        f"{current_name}#{current_version} "
        f"(baseline wr={baseline['win_rate']:.1%}, pnl={baseline['total_pnl']:.4f})"
    )


def _evaluate_strategy_candidate(trades: list[dict]):
    state = _load_strategy_state()
    active = state.get("active")
    candidate = state.get("candidate")
    if not active or not candidate:
        return

    candidate_trades = [t for t in trades if t.get("strategy_version") == candidate.get("version")]
    stats = _trade_stats(candidate_trades)
    min_trades = int(candidate.get("thresholds", {}).get("min_trades", STRATEGY_EVAL["min_trades"]))
    promote_wr = float(candidate.get("thresholds", {}).get("promote_win_rate", STRATEGY_EVAL["promote_win_rate"]))
    min_avg_pnl = float(candidate.get("thresholds", {}).get("min_avg_pnl", STRATEGY_EVAL["min_avg_pnl"]))
    min_total_pnl = float(candidate.get("thresholds", {}).get("min_total_pnl", STRATEGY_EVAL["min_total_pnl"]))
    max_drawdown = float(candidate.get("thresholds", {}).get("max_drawdown", STRATEGY_EVAL["max_drawdown"]))
    min_win_rate_delta = float(candidate.get("thresholds", {}).get("min_win_rate_delta", STRATEGY_EVAL["min_win_rate_delta"]))

    if stats["total"] < min_trades:
        log.info(
            "전략 후보 평가 대기: "
            f"{candidate.get('name')}#{candidate.get('version')} "
            f"trades={stats['total']}/{min_trades}"
        )
        return

    baseline = candidate.get("baseline", {})
    passed = (
        stats["win_rate"] >= promote_wr
        and stats["avg_pnl"] >= min_avg_pnl
        and stats["total_pnl"] >= min_total_pnl
        and stats["max_drawdown"] <= max_drawdown
        and stats["win_rate"] >= float(baseline.get("win_rate", 0.0)) + min_win_rate_delta
        and stats["avg_pnl"] >= float(baseline.get("avg_pnl", -999.0))
    )

    history = state.setdefault("history", [])

    if passed:
        active_snapshot = _snapshot_path(candidate.get("name", "strategy"), candidate.get("version", "unknown"), "active")
        snapshot_text = _read_text(candidate.get("snapshot_path")) or STRATEGY_CFG.read_text(encoding="utf-8")
        _write_snapshot(active_snapshot, snapshot_text)
        history.append({
            "action": "promote",
            "at": _now_kst_iso(),
            "from_version": active.get("version"),
            "to_version": candidate.get("version"),
            "stats": stats,
            "baseline": baseline,
        })
        state["active"] = {
            "name": candidate.get("name"),
            "version": candidate.get("version"),
            "snapshot_path": str(active_snapshot),
            "promoted_at": _now_kst_iso(),
        }
        state["candidate"] = None
        _save_strategy_state(state)
        log.info(
            "전략 후보 유지 확정: "
            f"{candidate.get('name')}#{candidate.get('version')} "
            f"wr={stats['win_rate']:.1%} avg={stats['avg_pnl']:.4f} "
            f"pnl={stats['total_pnl']:.4f} dd={stats['max_drawdown']:.4f}"
        )
        return

    active_text = _read_text(active.get("snapshot_path"))
    if active_text is not None:
        STRATEGY_CFG.write_text(active_text, encoding="utf-8")
    history.append({
        "action": "rollback",
        "at": _now_kst_iso(),
        "from_version": candidate.get("version"),
        "to_version": active.get("version"),
        "stats": stats,
        "baseline": baseline,
    })
    state["candidate"] = None
    _save_strategy_state(state)
    log.warning(
        "전략 후보 롤백: "
        f"{candidate.get('name')}#{candidate.get('version')} "
        f"wr={stats['win_rate']:.1%} avg={stats['avg_pnl']:.4f} "
        f"pnl={stats['total_pnl']:.4f} dd={stats['max_drawdown']:.4f}"
    )


def _load_trade_events() -> list[dict]:
    trade_log = DATA_DIR / "trade_log.json"
    if trade_log.exists():
        try:
            raw = json.loads(trade_log.read_text())
            if isinstance(raw, list):
                trades = [t for t in (_normalize_trade(x) for x in raw) if t]
                if trades:
                    return trades
        except Exception:
            pass

    trades = []
    for src in TRADE_SOURCES:
        if not src.exists():
            continue
        try:
            for line in src.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                t = _normalize_trade(json.loads(line))
                if t:
                    trades.append(t)
        except Exception as e:
            log.warning(f"trade source parse failed: {src} ({e})")

    if trades:
        trades.sort(key=lambda x: x.get("ts") or "")
        trade_log.write_text(json.dumps(trades, ensure_ascii=False, indent=2), encoding="utf-8")
    return trades

def _load_analytics(trades: Optional[list[dict]] = None) -> dict:
    trades = trades if trades is not None else _load_trade_events()
    total = len(trades)
    wins  = [t for t in trades if t.get("pnl_pct",0) > 0]
    pattern_stats, by_strategy = {}, {}
    for t in trades:
        p = t.get("pattern","미분류")
        pattern_stats.setdefault(p, {"wins":0,"total":0,"pnl":[]})
        pattern_stats[p]["total"] += 1
        if t.get("pnl_pct",0) > 0: pattern_stats[p]["wins"] += 1
        pattern_stats[p]["pnl"].append(t.get("pnl_pct",0))
        s = t.get("strategy","unknown")
        by_strategy.setdefault(s, {"total":0,"wins":0,"pnl":[]})
        by_strategy[s]["total"] += 1
        if t.get("pnl_pct",0) > 0: by_strategy[s]["wins"] += 1
        by_strategy[s]["pnl"].append(t.get("pnl_pct",0))
    for d in list(pattern_stats.values()) + list(by_strategy.values()):
        d["win_rate"] = d["wins"] / max(d["total"],1)
        pnls = d["pnl"]; avg = sum(pnls)/max(len(pnls),1)
        std  = (sum((x-avg)**2 for x in pnls)/max(len(pnls),1))**.5
        d["sharpe"] = avg / max(std, 0.001)
    return {"total_trades":total,"win_rate":len(wins)/max(total,1),
            "total_pnl":sum(t.get("pnl_pct",0) for t in trades),
            "pattern_stats":pattern_stats,"by_strategy":by_strategy,"recent_trades":trades[-20:]}

def _review_previous_day(analytics: dict):
    log.info("03:00 전날 성과 리뷰")
    log.info(f"  trades={analytics['total_trades']} win_rate={analytics['win_rate']:.1%} pnl={analytics['total_pnl']:.4f}")

def _prune_underperformers():
    log.info("03:10 하위 전략 백업(폐기 없음)")
    retired_file = DATA_DIR / "retired_strategies.json"
    try:
        retired = json.loads(retired_file.read_text()) if retired_file.exists() else []
    except Exception:
        retired = []

    changed = False
    for s in pool.pool:
        total = s.get("total_trades", 0)
        wr = s.get("wins", 0) / max(total, 1)
        sharpe = s.get("sharpe", 0.0)
        if total >= 20 and (wr < 0.40 or sharpe < 0.0):
            s["active"] = False
            s["retired"] = True
            s["retired_reason"] = f"wr={wr:.2f}, sharpe={sharpe:.2f}"
            s["retired_at"] = datetime.now(KST).isoformat()
            retired.append({
                "name": s.get("name"),
                "reason": s["retired_reason"],
                "retired_at": s["retired_at"],
            })
            changed = True

    if changed:
        pool._save()
        retired_file.write_text(json.dumps(retired, ensure_ascii=False, indent=2), encoding="utf-8")
        log.info("  하위 전략은 삭제하지 않고 백업/비활성화 처리 완료")

def daily_cycle():
    log.info("="*55); log.info("▶ 정규 커스텀 사이클 시작 (03:00~04:10)"); log.info("="*55)
    trades = _load_trade_events()
    analytics = _load_analytics(trades)
    _maybe_generate_strategy_candidate(analytics)
    _sync_strategy_candidate(trades)
    _evaluate_strategy_candidate(trades)
    phase     = detect_phase(analytics)
    _review_previous_day(analytics)   # 03:00
    _prune_underperformers()          # 03:10

    log.info(f"03:20 LLM 새 전략 생성/진화 시작 (Phase {phase})")
    result = run_evolution_cycle(pool, analytics)

    log.info("03:40 백테스트 자동 실행 결과 집계")
    new_cnt = len(result.get('new_strategies', []))
    ind_cnt = len(result.get('new_indicators', []))
    log.info(f"  생성전략={new_cnt} 신규지표={ind_cnt}")

    log.info("04:00 검증 통과 전략 풀 등록 확인")
    active = pool.select_top(n=3)
    pool.set_active(active)

    log.info("04:10 당일 활성 전략 3개 선정")
    for a in active: log.info(f"  🟢 활성: {a['name']}")

    notify_evolution(phase, result.get("new_strategies", []), result.get("new_indicators", []), [])
    notify_daily_report(analytics["total_pnl"], analytics["win_rate"],
                        analytics["total_trades"], active, phase, analytics["total_trades"])
    log.info("✅ 정규 사이클 완료 (런던 킬존 전 준비)")

def emergency_check():
    f = DATA_DIR / "status.json"
    if not f.exists(): return
    try: status = json.loads(f.read_text())
    except: return
    ts = status.get("trade_state", {})
    checks = [
        (ts.get("consecutive_loss",0)  >= THRESHOLDS["consecutive_loss"],  "consecutive_loss", f"연속 손실 {ts.get('consecutive_loss',0)}회"),
        (ts.get("recent_win_rate_20",1) < THRESHOLDS["min_win_rate"],       "low_win_rate",     f"최근 승률 {ts.get('recent_win_rate_20',1):.0%}"),
        (ts.get("current_drawdown",0)  >  THRESHOLDS["max_drawdown"],       "max_drawdown",     f"낙폭 {ts.get('current_drawdown',0):.1%}"),
        (ts.get("market_vol_mult",1)   >= THRESHOLDS["vol_spike_mult"],     "vol_spike",        f"거래량 {ts.get('market_vol_mult',1):.1f}x"),
    ]
    for triggered, trigger, reason in checks:
        if not triggered:
            continue
        log.warning(f"🚨 긴급: {reason} (원칙: 실시간 LLM 호출 없음, 기록/보호조치만)")
        pool.deactivate_all()

        if trigger == 'max_drawdown':
            pool.set_trading_halt(True)
            action = "거래 중단"
            notify_emergency(trigger, reason, action)
            return

        if trigger in ('low_win_rate', 'vol_spike'):
            backup = pool.select_safest(n=1)
            if backup:
                pool.set_active(backup)
                action = "백업 전략 교체"
                log.info(f"  🔄 백업: {backup[0]['name']}")
            else:
                pool.set_trading_halt(True)
                action = "거래 중단"
            notify_emergency(trigger, reason, action)
            return

        # consecutive_loss
        action = "즉시 비활성화"
        notify_emergency(trigger, reason, action)
        return

def weekly_evolution_cycle():
    log.info("🧬 주간 진화 사이클 시작 (일요일 02:00)")
    analytics = _load_analytics()
    top2 = pool.select_top(n=2)
    bottom1 = pool.select_bottom(n=1)
    created = []

    if len(top2) >= 2:
      child = evolve_strategy(top2, [pool.get_stats(top2[0]['name']), pool.get_stats(top2[1]['name'])], mode='crossover')
      if child:
        child['name'] = child.get('name','weekly_cross') + '_w'
        v = validate_strategy(child)
        bt = run_backtest(child, days=30)
        if v and v.get('verdict') == 'APPROVED' and bt and bt.get('sharpe',0) >= 0.6:
          child['validation']=v; child['backtest']=bt; child['phase']='weekly'
          pool.add(child); created.append(child)

    if len(bottom1) >= 1:
      mutant = evolve_strategy(bottom1, [pool.get_stats(bottom1[0]['name'])], mode='mutate')
      if mutant:
        mutant['name'] = mutant.get('name','weekly_mut') + '_w'
        v = validate_strategy(mutant)
        bt = run_backtest(mutant, days=30)
        if v and v.get('verdict') == 'APPROVED' and bt and bt.get('sharpe',0) >= 0.5:
          mutant['validation']=v; mutant['backtest']=bt; mutant['phase']='weekly'
          pool.add(mutant); created.append(mutant)

    log.info(f"🧬 주간 진화 완료: 생성 {len(created)}개")


def _recent_window_stats(minutes: int = 60) -> dict:
    trades = _load_trade_events()
    now_dt = datetime.now(timezone.utc)
    cutoff = now_dt - timedelta(minutes=minutes)
    recent = []
    for t in trades:
        ts = t.get('ts')
        if not ts:
            continue
        try:
            dt = datetime.fromisoformat(str(ts).replace('Z', '+00:00'))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            if dt.astimezone(timezone.utc) >= cutoff:
                recent.append(t)
        except Exception:
            continue
    st = _trade_stats(recent)
    st['count'] = len(recent)
    st['window_minutes'] = minutes
    return st


def hourly_loss_improvement_cycle():
    """매 1시간 손실 체크 → 필요 시 자동 개선 루프 실행"""
    st = _recent_window_stats(60)
    # 데이터가 너무 적으면 과민반응 방지
    if st['count'] < 6:
        log.info(f"⏱️ 1h 체크: 표본 부족 n={st['count']} (skip)")
        return

    need_improve = (st['total_pnl'] < 0) or (st['win_rate'] < 0.45)
    log.info(
        f"⏱️ 1h 체크: n={st['count']} pnl={st['total_pnl']:.4f} "
        f"wr={st['win_rate']:.1%} improve={need_improve}"
    )
    if not need_improve:
        return

    # 실시간 감정 대응 방지 원칙은 유지하되, 사용자 요청으로 1시간 주기 개선 수행
    trades = _load_trade_events()
    analytics = _load_analytics(trades)
    generated = _maybe_generate_strategy_candidate(analytics, force=True)
    trades = _load_trade_events()
    _sync_strategy_candidate(trades)
    _evaluate_strategy_candidate(trades)

    state = _load_strategy_state()
    notify_emergency(
        "hourly_improve",
        f"1h 손실/저승률 감지 n={st['count']} pnl={st['total_pnl']:.4f} wr={st['win_rate']:.1%}",
        f"개선 루프 실행(generated={generated}) active={state.get('active')}"
    )
    log.info(
        f"🔧 1h 개선 완료 generated={generated} active={state.get('active')} "
        f"candidate={state.get('candidate')}"
    )


def manual_tune_and_evaluate():
    log.info("🛠️ 수동 전략 튜닝 시작")
    trades = _load_trade_events()
    analytics = _load_analytics(trades)
    generated = _maybe_generate_strategy_candidate(analytics, force=True)
    trades = _load_trade_events()
    _sync_strategy_candidate(trades)
    _evaluate_strategy_candidate(trades)
    state = _load_strategy_state()
    log.info(f"🛠️ 수동 전략 튜닝 완료 generated={generated} active={state.get('active')} candidate={state.get('candidate')}")

def start():
    scheduler = BackgroundScheduler(timezone="Asia/Seoul")
    scheduler.add_job(daily_cycle,     CronTrigger(hour=3,minute=0,timezone="Asia/Seoul"), id="daily_cycle",  misfire_grace_time=300)
    scheduler.add_job(weekly_evolution_cycle, CronTrigger(day_of_week='sun', hour=2, minute=0, timezone='Asia/Seoul'), id='weekly_cycle', misfire_grace_time=300)
    scheduler.add_job(hourly_loss_improvement_cycle, "interval", hours=1, id="hourly_improve")
    scheduler.add_job(emergency_check, "interval", minutes=5, id="emergency_check")
    scheduler.start()
    log.info("🚀 Volky 스케줄러 v2 시작")
    log.info("  📅 정규: 매일 03:00 KST (03:00→04:10 단계형 사이클)")
    log.info("  🧬 주간: 매주 일요일 02:00 KST (상위2 교배 + 하위1 돌연변이)")
    log.info("  🔧 개선: 1시간마다 손실/승률 체크 후 자동 개선 루프")
    log.info("  🚨 긴급: 5분마다 (실시간 LLM 호출 금지)")
    try:
        while True: time.sleep(60)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown(); log.info("스케줄러 종료")

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "manual_tune":
        manual_tune_and_evaluate()
    else:
        start()
