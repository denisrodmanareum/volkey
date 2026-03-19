"""
volky-bot / engine / surge_scalper.py

급등 스캘핑 통합 엔진
- 펌프덤프 필터
- 모멘텀 지속성 점수
- 급등 패턴 분류
- 시간SL + 부분청산
- 고점 추격 방지
- 멀티 코인 우선순위
- 사후 분석 데이터 수집
"""

import json
import time
import asyncio
import logging
import aiohttp
from datetime import datetime, timezone, timedelta
from pathlib import Path
from dataclasses import dataclass, field, asdict
from typing import Optional
from enum import Enum

# ── 설정 ─────────────────────────────────────────────
KST      = timezone(timedelta(hours=9))
DATA_DIR = Path(__file__).parent.parent / "data"
DATA_DIR.mkdir(exist_ok=True)

BINANCE_FAPI = "https://fapi.binance.com"

log = logging.getLogger("volky.surge")

# ══════════════════════════════════════════════════════
#  데이터 클래스
# ══════════════════════════════════════════════════════

class SurgePattern(Enum):
    ACCUMULATION = "축적형"    # 횡보 후 거래량 동반 돌파 ✅ 최선
    CONTINUATION = "연속형"    # 추세 중 눌림 후 재급등  ✅ 좋음
    NEWS         = "뉴스형"    # 거래량 폭발 + 빠른 반응  ⚠️ 주의
    PUMP         = "펌프형"    # 갑작스런 급등, 이유 불명  ❌ 위험
    UNKNOWN      = "미분류"

class SignalStatus(Enum):
    WATCHING  = "감시중"
    WAITING   = "눌림대기"
    READY     = "진입가능"
    ENTERED   = "진입완료"
    CLOSED    = "청산완료"
    REJECTED  = "진입거부"

@dataclass
class SurgeCandidate:
    symbol:          str
    detected_at:     str        # KST ISO
    detected_price:  float
    pct_change:      float      # 변화율 %
    vol_mult:        float      # 거래량 배수
    pattern:         SurgePattern = SurgePattern.UNKNOWN
    momentum_score:  int        = 0     # 0~100
    pump_risk:       int        = 0     # 0~100 (높을수록 위험)
    entry_zone_low:  float      = 0.0
    entry_zone_high: float      = 0.0
    status:          SignalStatus = SignalStatus.WATCHING
    reject_reason:   str        = ""
    klines:          list       = field(default_factory=list)
    # AI model scores (Layer 1 / Layer 2)
    moirai_anomaly_score: float = 0.0
    ai_confidence:   float      = 0.0
    ai_direction:    str        = ""

@dataclass
class SurgePosition:
    symbol:         str
    entry_price:    float
    qty:            float
    sl_price:       float
    tp1_price:      float       # 1차 TP (50% 청산)
    tp2_price:      float       # 2차 TP (나머지)
    time_sl_at:     str         # 시간SL 만료 시각
    partial_closed: bool        = False
    entered_at:     str         = ""
    pnl:            float       = 0.0

@dataclass
class SurgeResult:
    symbol:          str
    pattern:         str
    detected_price:  float
    entry_price:     float
    exit_price:      float
    pnl_pct:         float
    hold_minutes:    float
    momentum_score:  int
    pump_risk:       int
    entry_delay_sec: float      # 감지 후 진입까지 걸린 시간
    outcome:         str        # WIN / LOSS / TIME_SL / PARTIAL
    closed_at:       str


# ══════════════════════════════════════════════════════
#  설정값
# ══════════════════════════════════════════════════════
class SurgeConfig:
    # 감지 기준
    MIN_PCT_CHANGE   = 1.5      # 최소 변화율 %
    MIN_VOL_MULT     = 2.0      # 최소 거래량 배수
    SCAN_INTERVAL    = 30       # 스캔 주기 (초)

    # 모멘텀 점수 기준
    MOMENTUM_MIN     = 50       # 진입 허용 최소 점수

    # 펌프 리스크 기준
    PUMP_RISK_MAX    = 60       # 이 이상이면 진입 거부

    # 진입 구간 (급등봉 기준)
    ENTRY_PULLBACK_MIN = 0.30   # 급등봉 되돌림 최소 30%
    ENTRY_PULLBACK_MAX = 0.65   # 급등봉 되돌림 최대 65%

    # 리스크 관리
    TIME_SL_MINUTES  = 15       # 시간SL (15분 내 미달 → 청산)
    TP1_RATIO        = 0.005    # 1차 TP +0.5%
    TP2_RATIO        = 0.012    # 2차 TP +1.2%
    SL_RATIO         = 0.004    # SL -0.4%
    PARTIAL_QTY      = 0.5      # 1차 TP 시 50% 청산
    REENTRY_COOLDOWN = 600      # 재진입 쿨다운 (초)

    # 동시 포지션
    MAX_POSITIONS    = 3
    ORDER_USDT       = 40


# ══════════════════════════════════════════════════════
#  Binance API 헬퍼
# ══════════════════════════════════════════════════════
async def fetch_tickers(session: aiohttp.ClientSession) -> list:
    async with session.get(f"{BINANCE_FAPI}/fapi/v1/ticker/24hr") as r:
        return await r.json()

async def fetch_klines(session, symbol: str, interval="5m", limit=30) -> list:
    url = f"{BINANCE_FAPI}/fapi/v1/klines"
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    async with session.get(url, params=params) as r:
        return await r.json()

async def fetch_depth(session, symbol: str, limit=10) -> dict:
    url = f"{BINANCE_FAPI}/fapi/v1/depth"
    async with session.get(url, params={"symbol": symbol, "limit": limit}) as r:
        return await r.json()


# ══════════════════════════════════════════════════════
#  1. 펌프덤프 필터
# ══════════════════════════════════════════════════════
def calc_pump_risk(klines: list, ticker: dict, depth: dict) -> int:
    """
    펌프덤프 리스크 점수 계산 (0~100, 높을수록 위험)
    """
    score = 0

    if not klines or len(klines) < 5:
        return 50  # 데이터 부족 = 중간 리스크

    closes  = [float(k[4]) for k in klines]
    volumes = [float(k[5]) for k in klines]
    latest_vol = volumes[-1]
    avg_vol    = sum(volumes[:-1]) / max(len(volumes) - 1, 1)

    # ── 체크 1: 거래량 대비 가격 변화 비율
    # 거래량은 적은데 가격만 급등 → 세력 조작 의심
    pct = abs(float(ticker.get("priceChangePercent", 0)))
    vol_mult = latest_vol / avg_vol if avg_vol > 0 else 1
    price_vol_ratio = pct / max(vol_mult, 0.1)
    if price_vol_ratio > 3.0:    # 가격변화 >> 거래량변화
        score += 35

    # ── 체크 2: 단일봉 급등 (직전봉들은 조용)
    prev_changes = [abs(closes[i] - closes[i-1]) / closes[i-1] * 100
                    for i in range(1, len(closes)-1)]
    avg_prev_change = sum(prev_changes) / max(len(prev_changes), 1)
    last_change = abs(closes[-1] - closes[-2]) / closes[-2] * 100
    if last_change > avg_prev_change * 5:  # 직전 평균의 5배 이상 단일봉
        score += 25

    # ── 체크 3: 오더북 매도벽
    if depth and "asks" in depth and len(depth["asks"]) > 0:
        current_price = float(ticker.get("lastPrice", closes[-1]))
        asks = [(float(p), float(q)) for p, q in depth["asks"][:5]]
        # 현재가 +0.5% 이내 매도벽 두께
        near_sell_wall = sum(q for p, q in asks if p <= current_price * 1.005)
        total_ask_qty  = sum(q for _, q in asks)
        if total_ask_qty > 0 and near_sell_wall / total_ask_qty > 0.6:
            score += 20  # 근거리 매도벽 집중

    # ── 체크 4: 연속 상승 후 급등 (이미 과열)
    up_count = sum(1 for i in range(1, len(closes)) if closes[i] > closes[i-1])
    if up_count >= len(closes) * 0.8:   # 80% 이상 상승봉
        score += 20

    return min(score, 100)


# ══════════════════════════════════════════════════════
#  2. 모멘텀 점수
# ══════════════════════════════════════════════════════
def calc_momentum_score(klines: list, vol_mult: float) -> int:
    """
    모멘텀 지속성 점수 (0~100, 높을수록 좋음)
    """
    if not klines or len(klines) < 5:
        return 0

    score = 0
    closes  = [float(k[4]) for k in klines]
    highs   = [float(k[2]) for k in klines]
    lows    = [float(k[3]) for k in klines]
    volumes = [float(k[5]) for k in klines]

    # ── 점수 1: 연속 양봉 (최근 3봉)
    recent3 = [closes[i] > closes[i-1] for i in range(-3, 0)]
    consec_up = sum(recent3)
    score += consec_up * 10  # 최대 30점

    # ── 점수 2: 거래량 증가세
    if len(volumes) >= 3:
        vol_trend = (volumes[-1] > volumes[-2]) and (volumes[-2] > volumes[-3])
        if vol_trend:
            score += 20

    # ── 점수 3: 고점 돌파 중
    prev_high = max(highs[:-1])
    if closes[-1] > prev_high:
        score += 20

    # ── 점수 4: 거래량 배수
    if vol_mult >= 5:    score += 20
    elif vol_mult >= 3:  score += 12
    elif vol_mult >= 2:  score += 6

    # ── 점수 5: 저점 유지 (눌리지 않음)
    if lows[-1] > lows[-2]:
        score += 10

    return min(score, 100)


# ══════════════════════════════════════════════════════
#  3. 급등 패턴 분류
# ══════════════════════════════════════════════════════
def classify_pattern(klines: list, vol_mult: float, pump_risk: int) -> SurgePattern:
    if not klines or len(klines) < 10:
        return SurgePattern.UNKNOWN

    closes  = [float(k[4]) for k in klines]
    volumes = [float(k[5]) for k in klines]

    # 직전 10봉 변동성 (횡보 여부)
    prev10_range = (max(closes[-11:-1]) - min(closes[-11:-1])) / closes[-11] * 100
    latest_change = abs(closes[-1] - closes[-2]) / closes[-2] * 100

    # 거래량 증가세
    vol_increasing = all(volumes[-i] > volumes[-i-1] for i in range(1, 3))

    # 펌프형: 위험 점수 높음
    if pump_risk >= 55:
        return SurgePattern.PUMP

    # 축적형: 횡보(변동성 낮음) 후 거래량 동반 돌파
    if prev10_range < 2.0 and vol_mult >= 2.5 and vol_increasing:
        return SurgePattern.ACCUMULATION

    # 연속형: 이미 상승 중인 흐름에서 재급등
    up_ratio = sum(1 for i in range(1, 6) if closes[-i] > closes[-i-1]) / 5
    if up_ratio >= 0.6 and latest_change >= 1.0:
        return SurgePattern.CONTINUATION

    # 뉴스형: 갑작스러운 거래량 폭발 (축적/연속 아님)
    if vol_mult >= 4.0:
        return SurgePattern.NEWS

    return SurgePattern.UNKNOWN


# ══════════════════════════════════════════════════════
#  4. 진입 구간 계산
# ══════════════════════════════════════════════════════
def calc_entry_zone(klines: list, pattern: SurgePattern) -> tuple[float, float]:
    """
    급등봉 기준 눌림 진입 구간 계산
    축적형/연속형: 급등봉 저가~50% 되돌림
    뉴스형: 더 타이트하게 30% 이내
    """
    if not klines:
        return 0.0, 0.0

    surge_candle = klines[-1]
    surge_high = float(surge_candle[2])
    surge_low  = float(surge_candle[3])
    surge_range = surge_high - surge_low

    if pattern in (SurgePattern.ACCUMULATION, SurgePattern.CONTINUATION):
        low  = surge_low
        high = surge_low + surge_range * SurgeConfig.ENTRY_PULLBACK_MAX
    elif pattern == SurgePattern.NEWS:
        low  = surge_low + surge_range * SurgeConfig.ENTRY_PULLBACK_MIN
        high = surge_low + surge_range * 0.45
    else:
        # 펌프/미분류: 진입 구간 없음 (0,0 반환)
        return 0.0, 0.0

    return round(low, 4), round(high, 4)


# ══════════════════════════════════════════════════════
#  5. 후보 평가 통합
# ══════════════════════════════════════════════════════
async def evaluate_candidate(
    session: aiohttp.ClientSession,
    ticker: dict,
) -> Optional[SurgeCandidate]:

    symbol = ticker["symbol"]
    pct    = float(ticker["priceChangePercent"])
    price  = float(ticker["lastPrice"])

    # 캔들 + 오더북 병렬 조회
    klines, depth = await asyncio.gather(
        fetch_klines(session, symbol, "5m", 30),
        fetch_depth(session, symbol, 10),
        return_exceptions=True
    )
    if isinstance(klines, Exception) or isinstance(depth, Exception):
        return None

    # 거래량 배수
    vols = [float(k[5]) for k in klines]
    vol_mult = vols[-1] / (sum(vols[:-1]) / max(len(vols)-1, 1)) if vols else 1.0

    pump_risk       = calc_pump_risk(klines, ticker, depth)
    momentum_score  = calc_momentum_score(klines, vol_mult)
    pattern         = classify_pattern(klines, vol_mult, pump_risk)
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
        klines         = klines[-5:],  # 최근 5봉만 저장
    )

    # 진입 가능 여부 판정
    reject = _check_entry_conditions(candidate)
    if reject:
        candidate.status       = SignalStatus.REJECTED
        candidate.reject_reason = reject
    elif entry_low > 0:
        candidate.status = SignalStatus.WAITING  # 눌림 대기
    else:
        candidate.status = SignalStatus.WATCHING

    return candidate


def _check_entry_conditions(c: SurgeCandidate) -> str:
    """진입 불가 사유 반환 (없으면 빈 문자열)"""
    if c.pump_risk >= SurgeConfig.PUMP_RISK_MAX:
        return f"펌프 리스크 {c.pump_risk}점 (기준 {SurgeConfig.PUMP_RISK_MAX})"
    if c.momentum_score < SurgeConfig.MOMENTUM_MIN:
        return f"모멘텀 {c.momentum_score}점 (기준 {SurgeConfig.MOMENTUM_MIN})"
    if c.pattern == SurgePattern.PUMP:
        return "펌프형 패턴 — 진입 금지"
    if c.entry_zone_low == 0:
        return "진입 구간 계산 불가"
    return ""


# ══════════════════════════════════════════════════════
#  6. 우선순위 정렬
# ══════════════════════════════════════════════════════
def prioritize(candidates: list[SurgeCandidate]) -> list[SurgeCandidate]:
    """
    우선순위 = 모멘텀점수 × 패턴가중치 × (1 - 펌프리스크/100)
    """
    pattern_weight = {
        SurgePattern.ACCUMULATION: 1.0,
        SurgePattern.CONTINUATION: 0.85,
        SurgePattern.NEWS:         0.65,
        SurgePattern.PUMP:         0.0,
        SurgePattern.UNKNOWN:      0.5,
    }

    def priority_score(c: SurgeCandidate) -> float:
        if c.status == SignalStatus.REJECTED:
            return -1
        pw = pattern_weight.get(c.pattern, 0.5)
        safety = 1 - (c.pump_risk / 100)
        base = c.momentum_score * pw * safety
        # Boost by MOIRAI anomaly score (Layer 1)
        ai_boost = 1.0 + c.moirai_anomaly_score
        return base * ai_boost

    return sorted(candidates, key=priority_score, reverse=True)


# ══════════════════════════════════════════════════════
#  7. 리스크 관리 — SL/TP 계산
# ══════════════════════════════════════════════════════
def calc_risk_levels(entry_price: float, direction: str = "long") -> dict:
    if direction == "long":
        sl  = entry_price * (1 - SurgeConfig.SL_RATIO)
        tp1 = entry_price * (1 + SurgeConfig.TP1_RATIO)
        tp2 = entry_price * (1 + SurgeConfig.TP2_RATIO)
    else:
        sl  = entry_price * (1 + SurgeConfig.SL_RATIO)
        tp1 = entry_price * (1 - SurgeConfig.TP1_RATIO)
        tp2 = entry_price * (1 - SurgeConfig.TP2_RATIO)

    time_sl_at = datetime.now(KST) + timedelta(minutes=SurgeConfig.TIME_SL_MINUTES)

    return {
        "sl":         round(sl,  4),
        "tp1":        round(tp1, 4),
        "tp2":        round(tp2, 4),
        "time_sl_at": time_sl_at.isoformat(),
        "rr":         round(SurgeConfig.TP2_RATIO / SurgeConfig.SL_RATIO, 2),
    }


# ══════════════════════════════════════════════════════
#  8. 포지션 모니터 (시간SL + 부분청산)
# ══════════════════════════════════════════════════════
def check_position(pos: SurgePosition, current_price: float) -> dict:
    """
    현재 포지션 상태 체크
    반환: {"action": "hold|partial_close|full_close", "reason": "..."}
    """
    now = datetime.now(KST)
    time_sl = datetime.fromisoformat(pos.time_sl_at)

    # 시간SL 만료
    if now >= time_sl and not pos.partial_closed:
        return {"action": "full_close", "reason": f"시간SL 만료 ({SurgeConfig.TIME_SL_MINUTES}분)"}

    # SL 터치
    if current_price <= pos.sl_price:
        return {"action": "full_close", "reason": f"SL 터치 ({pos.sl_price})"}

    # 1차 TP (부분 청산)
    if not pos.partial_closed and current_price >= pos.tp1_price:
        return {"action": "partial_close", "reason": f"TP1 달성 (+{SurgeConfig.TP1_RATIO*100:.1f}%) → 50% 청산"}

    # 2차 TP
    if pos.partial_closed and current_price >= pos.tp2_price:
        return {"action": "full_close", "reason": f"TP2 달성 (+{SurgeConfig.TP2_RATIO*100:.1f}%) → 전량 청산"}

    return {"action": "hold", "reason": "홀딩 중"}


# ══════════════════════════════════════════════════════
#  9. 사후 분석 데이터 저장
# ══════════════════════════════════════════════════════
class SurgeAnalytics:
    def __init__(self):
        self.file = DATA_DIR / "surge_analytics.json"
        self.data = self._load()

    def _load(self) -> dict:
        if self.file.exists():
            try:
                return json.loads(self.file.read_text())
            except Exception:
                pass
        return {"results": [], "stats": {}}

    def save(self):
        self.file.write_text(json.dumps(self.data, ensure_ascii=False, indent=2))

    def record_result(self, result: SurgeResult):
        self.data["results"].append(asdict(result))
        self._update_stats()
        self.save()
        log.info(f"[분석] {result.symbol} {result.outcome} "
                 f"PnL={result.pnl_pct:+.2f}% "
                 f"패턴={result.pattern} "
                 f"진입지연={result.entry_delay_sec:.0f}s")

    def _update_stats(self):
        results = self.data["results"]
        if not results:
            return

        by_pattern = {}
        for r in results:
            p = r["pattern"]
            by_pattern.setdefault(p, {"wins": 0, "total": 0, "pnl": []})
            by_pattern[p]["total"] += 1
            if r["outcome"] == "WIN":
                by_pattern[p]["wins"] += 1
            by_pattern[p]["pnl"].append(r["pnl_pct"])

        self.data["stats"] = {
            p: {
                "win_rate":    v["wins"] / v["total"],
                "total":       v["total"],
                "avg_pnl":     sum(v["pnl"]) / len(v["pnl"]),
                "best_pnl":    max(v["pnl"]),
                "worst_pnl":   min(v["pnl"]),
            }
            for p, v in by_pattern.items()
        }

    def get_best_entry_delay(self) -> float:
        """WIN 케이스의 평균 진입 지연 시간 (초)"""
        wins = [r for r in self.data["results"] if r["outcome"] == "WIN"]
        if not wins:
            return 60.0
        return sum(r["entry_delay_sec"] for r in wins) / len(wins)

    def print_summary(self):
        stats = self.data.get("stats", {})
        print("\n📊 패턴별 통계")
        print("-" * 50)
        for pattern, s in stats.items():
            print(f"  {pattern:<10} "
                  f"승률={s['win_rate']:.0%}  "
                  f"건수={s['total']}  "
                  f"평균PnL={s['avg_pnl']:+.2f}%")


# ══════════════════════════════════════════════════════
#  10. 메인 스캔 루프
# ══════════════════════════════════════════════════════
class SurgeScalper:
    def __init__(self):
        self.config    = SurgeConfig()
        self.analytics = SurgeAnalytics()
        self.cooldowns: dict[str, float] = {}   # symbol → 마지막 진입 시각
        self.candidates: list[SurgeCandidate] = []
        self.running = False

    def _is_on_cooldown(self, symbol: str) -> bool:
        last = self.cooldowns.get(symbol, 0)
        return (time.time() - last) < SurgeConfig.REENTRY_COOLDOWN

    async def scan_once(self, session: aiohttp.ClientSession):
        tickers = await fetch_tickers(session)

        # USDT 선물 + 최소 조건 필터
        candidates_raw = [
            t for t in tickers
            if t["symbol"].endswith("USDT")
            and "_" not in t["symbol"]
            and abs(float(t["priceChangePercent"])) >= SurgeConfig.MIN_PCT_CHANGE
            and not self._is_on_cooldown(t["symbol"])
        ]

        # 상위 30개만 상세 평가 (API 부하 방지)
        top30 = sorted(
            candidates_raw,
            key=lambda t: abs(float(t["priceChangePercent"])),
            reverse=True
        )[:30]

        # 병렬 평가
        tasks = [evaluate_candidate(session, t) for t in top30]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        self.candidates = [
            r for r in results
            if isinstance(r, SurgeCandidate)
        ]

        # 우선순위 정렬
        self.candidates = prioritize(self.candidates)

        # 상태 로그
        ready    = [c for c in self.candidates if c.status == SignalStatus.WAITING]
        rejected = [c for c in self.candidates if c.status == SignalStatus.REJECTED]

        log.info(
            f"[스캔] 후보={len(self.candidates)}  "
            f"진입대기={len(ready)}  "
            f"거부={len(rejected)}"
        )

        # 상위 후보 출력
        for i, c in enumerate(self.candidates[:5]):
            if c.status == SignalStatus.REJECTED:
                continue
            log.info(
                f"  [{i+1}] {c.symbol:<12} "
                f"변화={c.pct_change:+.1f}%  "
                f"거래량={c.vol_mult:.1f}x  "
                f"패턴={c.pattern.value}  "
                f"모멘텀={c.momentum_score}  "
                f"펌프리스크={c.pump_risk}  "
                f"상태={c.status.value}"
            )

        # status.json 업데이트
        self._save_status()

    def _save_status(self):
        status_file = DATA_DIR / "surge_status.json"
        out = {
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
                    "moirai_anomaly_score": round(c.moirai_anomaly_score, 3),
                    "ai_confidence":  round(c.ai_confidence, 3),
                    "ai_direction":   c.ai_direction,
                }
                for c in self.candidates[:20]  # 상위 20개
            ],
            "analytics_summary": self.analytics.data.get("stats", {}),
        }
        status_file.write_text(json.dumps(out, ensure_ascii=False, indent=2))

    async def run(self):
        self.running = True
        log.info("🚀 SurgeScalper 시작")
        async with aiohttp.ClientSession() as session:
            while self.running:
                try:
                    await self.scan_once(session)
                except Exception as e:
                    log.error(f"스캔 오류: {e}")
                await asyncio.sleep(SurgeConfig.SCAN_INTERVAL)

    def stop(self):
        self.running = False
        log.info("SurgeScalper 종료")


# ══════════════════════════════════════════════════════
#  실행
# ══════════════════════════════════════════════════════
if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s"
    )
    scalper = SurgeScalper()
    try:
        asyncio.run(scalper.run())
    except KeyboardInterrupt:
        scalper.stop()
        scalper.analytics.print_summary()
