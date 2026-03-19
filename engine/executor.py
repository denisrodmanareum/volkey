"""
volky-bot / engine / executor.py

Binance 선물 실주문 실행기
- ISOLATED 마진, 레버리지 설정
- 진입/청산/부분청산
- 주문 상태 추적
- Telegram 알림 연동
"""

import hmac
import time
import hashlib
import logging
import requests
from urllib.parse import urlencode
from pathlib import Path
from typing import Optional

log = logging.getLogger("volky.executor")

BINANCE_FAPI = "https://fapi.binance.com"

# ── 설정 (환경변수 또는 config.json에서 로드) ─────
import json
_cfg_file = Path(__file__).parent.parent / "config.json"

def _load_config() -> dict:
    if _cfg_file.exists():
        return json.loads(_cfg_file.read_text())
    return {}

cfg = _load_config()
API_KEY    = cfg.get("binance_api_key", "")
API_SECRET = cfg.get("binance_api_secret", "")
LEVERAGE   = cfg.get("leverage", 3)
ORDER_USDT = cfg.get("order_notional_usdt", 40)
DRY_RUN    = cfg.get("dry_run", True)   # True = 페이퍼 트레이딩


# ══════════════════════════════════════════════════
#  서명 / 요청 헬퍼
# ══════════════════════════════════════════════════
def _sign(params: dict) -> str:
    query = urlencode(params)
    return hmac.new(
        API_SECRET.encode(), query.encode(), hashlib.sha256
    ).hexdigest()

def _request(method: str, path: str, params: dict = None, signed=True) -> dict:
    params = params or {}
    if signed:
        params["timestamp"] = int(time.time() * 1000)
        params["signature"] = _sign(params)

    url     = BINANCE_FAPI + path
    headers = {"X-MBX-APIKEY": API_KEY}

    try:
        if method == "GET":
            r = requests.get(url, params=params, headers=headers, timeout=10)
        elif method == "POST":
            r = requests.post(url, params=params, headers=headers, timeout=10)
        elif method == "DELETE":
            r = requests.delete(url, params=params, headers=headers, timeout=10)
        else:
            return {}
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.error(f"[API] {method} {path} 오류: {e}")
        return {}


# ══════════════════════════════════════════════════
#  설정 초기화
# ══════════════════════════════════════════════════
def init_symbol(symbol: str):
    """레버리지 + 마진 모드 설정"""
    if DRY_RUN:
        return

    # 마진 모드 ISOLATED
    _request("POST", "/fapi/v1/marginType", {
        "symbol":     symbol,
        "marginType": "ISOLATED",
    })
    # 레버리지
    _request("POST", "/fapi/v1/leverage", {
        "symbol":   symbol,
        "leverage": LEVERAGE,
    })
    log.info(f"[INIT] {symbol} ISOLATED {LEVERAGE}x 설정 완료")


# ══════════════════════════════════════════════════
#  주문 실행
# ══════════════════════════════════════════════════
def get_price(symbol: str) -> float:
    r = _request("GET", "/fapi/v1/ticker/price", {"symbol": symbol}, signed=False)
    return float(r.get("price", 0))

def get_qty_precision(symbol: str) -> int:
    r = _request("GET", "/fapi/v1/exchangeInfo", signed=False)
    for s in r.get("symbols", []):
        if s["symbol"] == symbol:
            for f in s["filters"]:
                if f["filterType"] == "LOT_SIZE":
                    step = f["stepSize"]
                    return max(0, len(step.rstrip("0").split(".")[-1]))
    return 3

def calc_qty(symbol: str, usdt: float, price: float) -> float:
    precision = get_qty_precision(symbol)
    qty = (usdt * LEVERAGE) / price
    factor = 10 ** precision
    return int(qty * factor) / factor


def open_position(
    symbol:    str,
    side:      str,     # "long" | "short"
    sl_price:  float,
    tp_price:  float,
    usdt:      float = None,
) -> Optional[dict]:
    """
    시장가 진입 + SL/TP OCO 주문
    """
    usdt = usdt or ORDER_USDT
    price = get_price(symbol)
    if not price:
        return None

    qty = calc_qty(symbol, usdt, price)
    binance_side = "BUY" if side == "long" else "SELL"
    close_side   = "SELL" if side == "long" else "BUY"

    log.info(
        f"[{'DRY' if DRY_RUN else 'LIVE'}] {symbol} {side.upper()} "
        f"qty={qty} price≈{price} SL={sl_price} TP={tp_price}"
    )

    if DRY_RUN:
        return {
            "symbol":    symbol,
            "side":      side,
            "qty":       qty,
            "entry":     price,
            "sl":        sl_price,
            "tp":        tp_price,
            "order_id":  f"DRY_{int(time.time())}",
            "dry_run":   True,
        }

    init_symbol(symbol)

    # 진입 주문
    entry_order = _request("POST", "/fapi/v1/order", {
        "symbol":   symbol,
        "side":     binance_side,
        "type":     "MARKET",
        "quantity": qty,
    })
    if not entry_order.get("orderId"):
        log.error(f"[EXEC] 진입 실패: {entry_order}")
        return None

    order_id = entry_order["orderId"]
    log.info(f"[EXEC] 진입 완료 orderId={order_id}")

    # SL 주문
    _request("POST", "/fapi/v1/order", {
        "symbol":           symbol,
        "side":             close_side,
        "type":             "STOP_MARKET",
        "stopPrice":        round(sl_price, 4),
        "closePosition":    "true",
        "timeInForce":      "GTE_GTC",
    })

    # TP 주문
    _request("POST", "/fapi/v1/order", {
        "symbol":           symbol,
        "side":             close_side,
        "type":             "TAKE_PROFIT_MARKET",
        "stopPrice":        round(tp_price, 4),
        "closePosition":    "true",
        "timeInForce":      "GTE_GTC",
    })

    return {
        "symbol":   symbol,
        "side":     side,
        "qty":      qty,
        "entry":    price,
        "sl":       sl_price,
        "tp":       tp_price,
        "order_id": order_id,
    }


def close_position(symbol: str, side: str, qty: float, reason: str = "") -> bool:
    """포지션 청산"""
    close_side = "SELL" if side == "long" else "BUY"
    log.info(f"[{'DRY' if DRY_RUN else 'LIVE'}] {symbol} 청산 qty={qty} ({reason})")

    if DRY_RUN:
        return True

    r = _request("POST", "/fapi/v1/order", {
        "symbol":           symbol,
        "side":             close_side,
        "type":             "MARKET",
        "quantity":         qty,
        "reduceOnly":       "true",
    })
    return bool(r.get("orderId"))


def partial_close(symbol: str, side: str, total_qty: float, ratio: float = 0.5) -> bool:
    """부분 청산 (기본 50%)"""
    qty = round(total_qty * ratio, 3)
    return close_position(symbol, side, qty, reason=f"부분청산 {ratio:.0%}")


def cancel_all_orders(symbol: str):
    """해당 심볼 모든 주문 취소"""
    if DRY_RUN:
        return
    _request("DELETE", "/fapi/v1/allOpenOrders", {"symbol": symbol})
    log.info(f"[EXEC] {symbol} 주문 전체 취소")


def get_positions() -> list:
    """현재 오픈 포지션 조회"""
    r = _request("GET", "/fapi/v2/positionRisk")
    return [
        {
            "symbol":        p["symbol"],
            "side":          "long" if float(p["positionAmt"]) > 0 else "short",
            "qty":           abs(float(p["positionAmt"])),
            "entry":         float(p["entryPrice"]),
            "mark":          float(p["markPrice"]),
            "unrealized_pnl":float(p["unRealizedProfit"]),
        }
        for p in r
        if abs(float(p.get("positionAmt", 0))) > 0
    ]


def get_account_balance() -> float:
    """USDT 잔고 조회"""
    r = _request("GET", "/fapi/v2/balance")
    for asset in r:
        if asset.get("asset") == "USDT":
            return float(asset.get("availableBalance", 0))
    return 0.0
