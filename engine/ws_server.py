"""
volky-bot / engine / ws_server.py

WebSocket 실시간 신호 서버
- 바이낸스 선물 스트림 수신
- 급등 감지 신호 → 대시보드 실시간 푸시
- 포지션 상태 실시간 업데이트
"""

import asyncio
import json
import logging
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
import websockets
import aiohttp

from engine.surge_scalper import (
    SurgeConfig, SurgeCandidate, SignalStatus,
    calc_pump_risk, calc_momentum_score,
    classify_pattern, calc_entry_zone,
    prioritize, fetch_klines, fetch_depth,
)
from engine.executor   import open_position, close_position, partial_close, get_positions, get_account_balance, DRY_RUN
from engine.strategy_pool import StrategyPool
from engine.telegram_bot  import notify_entry, notify_close, notify_surge_detected
from llm.strategy_creator  import judge_signal

log = logging.getLogger("volky.ws_server")
KST = timezone(timedelta(hours=9))

DATA_DIR = Path(__file__).parent.parent / "data"
DATA_DIR.mkdir(exist_ok=True)

BINANCE_WS   = "wss://fstream.binance.com"
WS_HOST      = "0.0.0.0"
WS_PORT      = 8765

pool = StrategyPool(DATA_DIR / "strategy_pool.json")

# 연결된 대시보드 클라이언트
_dashboard_clients: set = set()

# 활성 포지션 트래커
_positions: dict[str, dict] = {}


# ══════════════════════════════════════════════════
#  대시보드 WebSocket 서버
# ══════════════════════════════════════════════════
async def dashboard_handler(ws):
    _dashboard_clients.add(ws)
    log.info(f"[WS] 대시보드 연결 ({len(_dashboard_clients)}개)")
    try:
        # 연결 즉시 현재 상태 전송
        await ws.send(json.dumps(_build_status()))
        async for _ in ws:
            pass
    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        _dashboard_clients.discard(ws)
        log.info(f"[WS] 대시보드 연결 해제 ({len(_dashboard_clients)}개)")


async def broadcast(data: dict):
    """모든 대시보드 클라이언트에 브로드캐스트"""
    if not _dashboard_clients:
        return
    msg = json.dumps(data, ensure_ascii=False)
    dead = set()
    for ws in _dashboard_clients:
        try:
            await ws.send(msg)
        except Exception:
            dead.add(ws)
    _dashboard_clients -= dead


def _build_status() -> dict:
    """status.json 형식으로 현재 상태 빌드"""
    positions = get_positions()

    # 실현 손익은 파일에서 읽기
    pnl_file = DATA_DIR / "pnl.json"
    pnl = {"realized_gross": 0, "commission": 0,
           "realized_net": 0, "unrealized": 0, "total": 0}
    if pnl_file.exists():
        try:
            pnl = json.loads(pnl_file.read_text())
        except Exception:
            pass

    unrealized = sum(p.get("unrealized_pnl", 0) for p in positions)
    pnl["unrealized"] = round(unrealized, 4)
    pnl["total"]      = round(pnl["realized_net"] + unrealized, 4)

    from engine.executor import LEVERAGE, ORDER_USDT
    return {
        "updated_at": datetime.now(KST).isoformat(),
        "config": {
            "margin_mode":        "ISOLATED",
            "leverage":           LEVERAGE,
            "max_positions":      SurgeConfig.MAX_POSITIONS,
            "order_notional_usdt":ORDER_USDT,
            "dry_run":            DRY_RUN,
        },
        "pnl": pnl,
        "positions": [
            {
                "symbol":        p["symbol"],
                "side":          p["side"].upper(),
                "qty":           p["qty"],
                "entry":         p["entry"],
                "mark":          p["mark"],
                "unrealized_pnl":p["unrealized_pnl"],
                "order_id":      None,
            }
            for p in positions
        ],
        "trade_state": {
            "consecutive_loss":      _get_consecutive_loss(),
            "recent_win_rate_20":    _get_recent_wr(20),
            "current_drawdown":      _get_drawdown(),
            "market_vol_mult":       1.0,
        },
    }


# ══════════════════════════════════════════════════
#  바이낸스 실시간 스트림 처리
# ══════════════════════════════════════════════════
_kline_cache: dict[str, list] = {}   # symbol → 최근 캔들 캐시
_surge_candidates: list = []


async def binance_stream_worker():
    """
    바이낸스 선물 !miniTicker 스트림 수신
    급등 코인 실시간 감지
    """
    url = f"{BINANCE_WS}/stream?streams=!miniTicker@arr"
    log.info(f"[STREAM] 바이낸스 연결: {url}")

    while True:
        try:
            async with websockets.connect(url, ping_interval=20) as ws:
                async with aiohttp.ClientSession() as session:
                    async for raw in ws:
                        msg = json.loads(raw)
                        tickers = msg.get("data", [])
                        await _process_tickers(tickers, session)
        except Exception as e:
            log.error(f"[STREAM] 연결 끊김: {e} — 5초 후 재연결")
            await asyncio.sleep(5)


async def _process_tickers(tickers: list, session: aiohttp.ClientSession):
    """실시간 티커 처리 — 급등 감지 + 신호 판단"""
    global _surge_candidates

    # 급등 후보 필터
    candidates_raw = [
        t for t in tickers
        if t.get("s", "").endswith("USDT")
        and "_" not in t.get("s", "")
        and abs(float(t.get("P", 0))) >= SurgeConfig.MIN_PCT_CHANGE
    ]

    if not candidates_raw:
        return

    # 상위 10개만 처리 (스트림은 매 초 들어오므로 가볍게)
    top10 = sorted(
        candidates_raw,
        key=lambda t: abs(float(t.get("P", 0))),
        reverse=True
    )[:10]

    new_candidates = []
    for t in top10:
        symbol   = t["s"]
        pct      = float(t["P"])
        price    = float(t["c"])

        # 캔들 캐시 (10초마다 갱신)
        now = time.time()
        cached = _kline_cache.get(symbol, {})
        if not cached or now - cached.get("ts", 0) > 10:
            try:
                klines = await fetch_klines(session, symbol, "5m", 20)
                depth  = await fetch_depth(session, symbol, 10)
                _kline_cache[symbol] = {"klines": klines, "depth": depth, "ts": now}
            except Exception:
                continue
        else:
            klines = cached["klines"]
            depth  = cached["depth"]

        vols     = [float(k[5]) for k in klines]
        vol_mult = vols[-1] / (sum(vols[:-1]) / max(len(vols)-1, 1)) if vols else 1.0

        if vol_mult < SurgeConfig.MIN_VOL_MULT:
            continue

        pump_risk      = calc_pump_risk(klines, {"lastPrice": price, "priceChangePercent": pct}, depth)
        momentum_score = calc_momentum_score(klines, vol_mult)
        pattern        = classify_pattern(klines, vol_mult, pump_risk)
        entry_low, entry_high = calc_entry_zone(klines, pattern)

        candidate = SurgeCandidate(
            symbol         = symbol,
            detected_at    = datetime.now(KST).isoformat(),
            detected_price = price,
            pct_change     = pct,
            vol_mult       = vol_mult,
            pattern        = pattern,
            momentum_score = momentum_score,
            pump_risk      = pump_risk,
            entry_zone_low = entry_low,
            entry_zone_high= entry_high,
            klines         = klines[-5:],
        )

        new_candidates.append(candidate)

        # 실시간 알림 (모멘텀 70+ 이상만)
        if momentum_score >= 70 and pump_risk < 40:
            notify_surge_detected(symbol, pct, vol_mult, pattern.value, momentum_score)

    if new_candidates:
        _surge_candidates = prioritize(new_candidates)
        await _check_entry_signals(session)
        await _broadcast_surge_update()


async def _check_entry_signals(session: aiohttp.ClientSession):
    """
    급등 후보 → LLM 신호 판단 → 실주문
    """
    if len(_positions) >= SurgeConfig.MAX_POSITIONS:
        return

    active_strategies = pool.select_top(n=1)
    if not active_strategies:
        return
    strategy = active_strategies[0]

    for c in _surge_candidates[:3]:
        if c.status != SignalStatus.WAITING:
            continue
        if c.pump_risk >= SurgeConfig.PUMP_RISK_MAX:
            continue
        if c.momentum_score < SurgeConfig.MOMENTUM_MIN:
            continue
        if c.symbol in _positions:
            continue

        # LLM 신호 판단 (Qwen3.5-9b — 빠른 응답)
        market_data = {
            "symbol":    c.symbol,
            "timeframe": "5m",
            "current_price": c.detected_price,
            "candles":   c.klines,
            "indicators": {
                "vol_mult": c.vol_mult,
                "momentum": c.momentum_score,
                "pump_risk": c.pump_risk,
            },
            "session": _get_session(),
        }

        signal = judge_signal(strategy, market_data)
        if not signal or signal.get("signal") == "NO_SIGNAL":
            continue

        direction = signal["signal"].lower()   # "long" | "short"
        entry     = signal.get("entry_price", c.detected_price)
        sl        = signal.get("sl_price",    c.entry_zone_low)
        tp        = signal.get("tp_price",    c.entry_zone_high)

        # 실주문
        order = open_position(c.symbol, direction, sl, tp)
        if order:
            _positions[c.symbol] = {
                "order":         order,
                "candidate":     c,
                "strategy":      strategy.get("name", ""),
                "partial_closed":False,
                "entered_at":    time.time(),
            }
            notify_entry(
                c.symbol, direction, entry, sl, tp,
                c.pattern.value, c.momentum_score,
                strategy.get("name", ""), DRY_RUN
            )
            log.info(f"[ENTRY] {c.symbol} {direction.upper()} entry={entry}")

        if len(_positions) >= SurgeConfig.MAX_POSITIONS:
            break


async def _check_position_exits():
    """포지션 청산 조건 주기적 체크"""
    while True:
        await asyncio.sleep(3)
        if not _positions:
            continue

        for symbol, pos_info in list(_positions.items()):
            order    = pos_info["order"]
            side     = order["side"]
            entry    = order["entry"]
            sl       = order["sl"]
            tp1      = entry * (1 + SurgeConfig.TP1_RATIO) if side == "long" else entry * (1 - SurgeConfig.TP1_RATIO)
            tp2      = order["tp"]
            time_sl  = pos_info["entered_at"] + SurgeConfig.TIME_SL_MINUTES * 60

            # 현재가 조회
            try:
                async with aiohttp.ClientSession() as s:
                    async with s.get(
                        f"https://fapi.binance.com/fapi/v1/ticker/price",
                        params={"symbol": symbol}
                    ) as r:
                        price = float((await r.json())["price"])
            except Exception:
                continue

            reason = None
            action = None

            # 시간SL
            if time.time() >= time_sl:
                reason = f"시간SL ({SurgeConfig.TIME_SL_MINUTES}분)"
                action = "full"

            # SL
            elif (side == "long"  and price <= sl) or (side == "short" and price >= sl):
                reason = f"SL 터치 ({sl})"
                action = "full"

            # TP1 (부분청산)
            elif not pos_info["partial_closed"]:
                if (side == "long" and price >= tp1) or (side == "short" and price <= tp1):
                    reason = f"TP1 달성"
                    action = "partial"

            # TP2
            elif pos_info["partial_closed"]:
                if (side == "long" and price >= tp2) or (side == "short" and price <= tp2):
                    reason = f"TP2 달성"
                    action = "full"

            if action == "partial":
                partial_close(symbol, side, order["qty"])
                pos_info["partial_closed"] = True
                pnl_pct = SurgeConfig.TP1_RATIO * 100
                notify_close(symbol, side, entry, price, pnl_pct, reason, DRY_RUN)
                log.info(f"[PARTIAL] {symbol} TP1 {price}")

            elif action == "full":
                close_position(symbol, side, order["qty"], reason)
                pnl_pct = (price - entry) / entry * 100 if side == "long" else (entry - price) / entry * 100
                notify_close(symbol, side, entry, price, pnl_pct, reason, DRY_RUN)
                _record_trade(symbol, side, entry, price, pnl_pct, pos_info)
                del _positions[symbol]
                log.info(f"[CLOSE] {symbol} {reason} pnl={pnl_pct:+.2f}%")


async def _broadcast_surge_update():
    """급등 스캐너 결과 대시보드에 브로드캐스트"""
    data = {
        "type":       "surge_update",
        "updated_at": datetime.now(KST).isoformat(),
        "candidates": [
            {
                "symbol":         c.symbol,
                "pct_change":     c.pct_change,
                "vol_mult":       round(c.vol_mult, 2),
                "pattern":        c.pattern.value,
                "momentum_score": c.momentum_score,
                "pump_risk":      c.pump_risk,
                "entry_zone":     [c.entry_zone_low, c.entry_zone_high],
                "status":         c.status.value,
                "reject_reason":  c.reject_reason,
                "detected_at":    c.detected_at,
            }
            for c in _surge_candidates[:20]
        ],
    }
    # status.json에도 저장 (HTTP 폴링 지원)
    status = _build_status()
    (DATA_DIR / "status.json").write_text(
        json.dumps(status, ensure_ascii=False, indent=2)
    )
    (DATA_DIR / "surge_status.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2)
    )
    await broadcast(status)


# ══════════════════════════════════════════════════
#  헬퍼
# ══════════════════════════════════════════════════
def _get_session() -> str:
    h = datetime.now(timezone.utc).hour
    if  2 <= h <  5: return "london"
    if  7 <= h < 10: return "new_york"
    if 10 <= h < 12: return "london_close"
    return "off"

def _get_consecutive_loss() -> int:
    trades = _load_trades()
    count = 0
    for t in reversed(trades):
        if t.get("pnl_pct", 0) < 0:
            count += 1
        else:
            break
    return count

def _get_recent_wr(n: int) -> float:
    trades = _load_trades()[-n:]
    if not trades:
        return 1.0
    wins = sum(1 for t in trades if t.get("pnl_pct", 0) > 0)
    return wins / len(trades)

def _get_drawdown() -> float:
    trades = _load_trades()
    if not trades:
        return 0.0
    equity = 1000.0
    peak   = 1000.0
    max_dd = 0.0
    for t in trades:
        equity *= (1 + t.get("pnl_pct", 0) / 100)
        peak    = max(peak, equity)
        max_dd  = max(max_dd, (peak - equity) / peak)
    return max_dd

def _load_trades() -> list:
    f = DATA_DIR / "trade_log.json"
    if not f.exists():
        return []
    try:
        return json.loads(f.read_text())
    except Exception:
        return []

def _record_trade(symbol, side, entry, exit_p, pnl_pct, pos_info):
    trades = _load_trades()
    trades.append({
        "symbol":      symbol,
        "side":        side,
        "entry":       entry,
        "exit":        exit_p,
        "pnl_pct":     round(pnl_pct, 3),
        "pattern":     pos_info["candidate"].pattern.value,
        "momentum":    pos_info["candidate"].momentum_score,
        "pump_risk":   pos_info["candidate"].pump_risk,
        "strategy":    pos_info["strategy"],
        "closed_at":   datetime.now(KST).isoformat(),
    })
    (DATA_DIR / "trade_log.json").write_text(
        json.dumps(trades, ensure_ascii=False, indent=2)
    )


# ══════════════════════════════════════════════════
#  메인
# ══════════════════════════════════════════════════
async def main():
    log.info(f"🚀 Volky WS Server 시작 (port={WS_PORT})")
    log.info(f"   모드: {'📄 페이퍼' if DRY_RUN else '💰 실거래'}")

    await asyncio.gather(
        websockets.serve(dashboard_handler, WS_HOST, WS_PORT),
        binance_stream_worker(),
        _check_position_exits(),
    )

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s"
    )
    asyncio.run(main())
