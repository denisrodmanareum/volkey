"""
volky-bot / llm / evolution_engine.py

Phase 1: 기법 조합 창조
Phase 2: 파라미터 진화 (거래 50건+)
Phase 3: 지표 수식 자체 창조 (거래 200건+)

매일 03:00 KST 자동 실행 — scheduler.py에서 호출
"""

import json
import math
import time
import logging
import ast
import operator
import traceback
from pathlib import Path
from typing import Optional

from llm.strategy_creator import (
    call_llm, safe_parse_json, load_prompt,
    create_strategy, validate_strategy, evolve_strategy
)
from engine.backtester import run_backtest

log = logging.getLogger("volky.evolution")

DATA_DIR      = Path(__file__).parent.parent / "data"
INDICATOR_LIB = Path(__file__).parent.parent / "knowledge" / "indicators" / "custom_library.json"
DATA_DIR.mkdir(exist_ok=True)
INDICATOR_LIB.parent.mkdir(exist_ok=True)

# ── Phase 진입 기준 ───────────────────────────────
PHASE2_MIN_TRADES = 50
PHASE3_MIN_TRADES = 200


# ══════════════════════════════════════════════════
#  Phase 감지
# ══════════════════════════════════════════════════
def detect_phase(analytics: dict) -> int:
    total = analytics.get("total_trades", 0)
    if total >= PHASE3_MIN_TRADES:
        return 3
    elif total >= PHASE2_MIN_TRADES:
        return 2
    return 1


# ══════════════════════════════════════════════════
#  Phase 1 — 기법 조합 창조
# ══════════════════════════════════════════════════
def phase1_create(pool_size: int, analytics: dict) -> list[dict]:
    """
    트레이더 기법 DB 기반 새 전략 조합 생성
    패턴별 성과 데이터를 LLM에 제공해 방향성 유도
    """
    log.info("[Phase1] 기법 조합 창조 시작")

    stats = analytics.get("pattern_stats", {})
    perf_hint = ""
    if stats:
        best  = max(stats.items(), key=lambda x: x[1].get("win_rate", 0))
        worst = min(stats.items(), key=lambda x: x[1].get("win_rate", 0))
        perf_hint = (
            f"현재 최고 성과 패턴: {best[0]} (승률 {best[1]['win_rate']:.0%})\n"
            f"현재 최저 성과 패턴: {worst[0]} (승률 {worst[1]['win_rate']:.0%})\n"
            f"이 데이터를 참고해 더 나은 전략을 만드세요."
        )

    combos = [
        (["ICT", "Wyckoff"],    "mean_reversion"),
        (["Elder", "Williams"], "trend_following"),
        (["ICT", "Elder"],      "breakout"),
        (["Wyckoff", "Williams"], "mean_reversion"),
        (["ICT", "Williams"],   "breakout"),
    ]

    need   = max(1, 5 - pool_size)
    result = []

    for traders, style in combos[:need]:
        s = create_strategy(trader_focus=traders, style=style)
        if s:
            v = validate_strategy(s)
            if v and v.get("verdict") == "APPROVED":
                bt = run_backtest(s, days=30)
                if bt and bt["sharpe"] >= 0.8 and bt["win_rate"] >= 0.42:
                    s["backtest"]   = bt
                    s["validation"] = v
                    s["phase"]      = 1
                    result.append(s)
                    log.info(f"  ✅ Phase1 전략: {s['name']} Sharpe={bt['sharpe']:.2f}")

    return result


# ══════════════════════════════════════════════════
#  Phase 2 — 파라미터 진화
# ══════════════════════════════════════════════════
PARAM_EVOLVE_PROMPT = """
당신은 트레이딩 전략 파라미터 최적화 전문가입니다.
실제 거래 성과 데이터를 분석하여 전략의 파라미터를 개선하세요.

입력 데이터:
{data}

분석 요구사항:
1. 어떤 조건(condition)이 자주 실패하는가?
2. 어떤 파라미터 값이 성과에 영향을 미치는가?
3. 세션/시간대별 성과 차이가 있는가?

개선 방향:
- EMA 기간 조정 (예: 13→9 또는 13→21)
- 임계값 조정 (예: 거래량배수 2.0→2.5)  
- 필터 추가 또는 완화
- 세션 필터 조정

반드시 JSON 형식만 출력하세요 (기존 전략 JSON 형식 유지, 변경된 파라미터만 수정):
"""

def phase2_evolve_params(strategies: list, analytics: dict) -> list[dict]:
    """
    실패 조건 분석 → 파라미터 자동 조정
    """
    log.info("[Phase2] 파라미터 진화 시작")
    result = []

    for s in strategies[:3]:  # 상위 3개 전략 파라미터 진화
        name  = s.get("name", "")
        perf  = analytics.get("by_strategy", {}).get(name, {})
        if not perf:
            continue

        data = {
            "strategy":        s,
            "total_trades":    perf.get("total", 0),
            "win_rate":        perf.get("win_rate", 0),
            "failed_conditions": perf.get("failed_conditions", []),
            "best_session":    perf.get("best_session", ""),
            "avg_hold_min":    perf.get("avg_hold_min", 0),
            "avg_entry_delay": perf.get("avg_entry_delay_sec", 0),
        }

        raw = call_llm(
            PARAM_EVOLVE_PROMPT.format(data=json.dumps(data, ensure_ascii=False)),
            "파라미터를 최적화한 새 전략 JSON을 생성하세요.",
            temperature=0.3,
        )
        evolved = safe_parse_json(raw)
        if not evolved:
            continue

        evolved["name"]    = name + "_p2"
        evolved["phase"]   = 2
        evolved["parent"]  = name

        v = validate_strategy(evolved)
        if v and v.get("verdict") == "APPROVED":
            bt = run_backtest(evolved, days=30)
            if bt and bt["sharpe"] > (perf.get("sharpe", 0)):
                evolved["backtest"]   = bt
                evolved["validation"] = v
                result.append(evolved)
                log.info(f"  ✅ Phase2 개선: {evolved['name']} Sharpe={bt['sharpe']:.2f} (기존={perf.get('sharpe',0):.2f})")

    return result


# ══════════════════════════════════════════════════
#  Phase 3 — 지표 수식 자체 창조
# ══════════════════════════════════════════════════
INDICATOR_CREATE_PROMPT = """
당신은 퀀트 트레이딩 지표 개발자입니다.
기존 지표들의 성과 데이터를 분석하여 완전히 새로운 지표 수식을 창조하세요.

사용 가능한 변수 (반드시 이 변수만 사용):
- 가격: open, high, low, close, prev_close
- 이동평균: ema9, ema13, ema21, ema50, ema200
- 변동성: atr14, atr7, std20
- 거래량: volume, vol_mult, obv, vwap
- 모멘텀: rsi14, macd_line, macd_signal, williams_r, force_index
- 구조: upper_bb, lower_bb, pivot_high, pivot_low

성과 데이터:
{analytics}

요구사항:
1. 위 변수들만 사용한 Python 수식 작성
2. 결과값 범위: -1.0 ~ +1.0 (정규화 필수)
3. 수식이 직관적으로 의미 있어야 함
4. 기존 지표와 차별화된 새로운 관점

JSON 형식만 출력:
{
  "name": "지표명 (영문, 언더스코어)",
  "description": "지표의 핵심 아이디어 (한국어)",
  "formula": "python 수식 (한 줄)",
  "buy_threshold": 0.6,
  "sell_threshold": -0.6,
  "inspired_by": ["참고한 기존 지표명"],
  "expected_edge": "이 지표가 포착하는 시장 비효율성 설명"
}
"""

# ── 샌드박스 안전 실행 ────────────────────────────
ALLOWED_NAMES = {
    # 변수
    "open","high","low","close","prev_close",
    "ema9","ema13","ema21","ema50","ema200",
    "atr14","atr7","std20",
    "volume","vol_mult","obv","vwap",
    "rsi14","macd_line","macd_signal","williams_r","force_index",
    "upper_bb","lower_bb","pivot_high","pivot_low",
    # 수학 함수
    "abs","max","min","round","log","sqrt","tanh","sign",
}

SAFE_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Pow: operator.pow,
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}

def _safe_eval(formula: str, variables: dict) -> float:
    """
    화이트리스트 기반 안전한 수식 평가
    eval() 미사용 — AST 파싱으로 보안 검증
    """
    def _eval_node(node):
        if isinstance(node, ast.Constant):
            return float(node.value)
        elif isinstance(node, ast.Name):
            if node.id not in ALLOWED_NAMES:
                raise ValueError(f"허용되지 않은 변수: {node.id}")
            val = variables.get(node.id, 0.0)
            return float(val) if val is not None else 0.0
        elif isinstance(node, ast.BinOp):
            op = SAFE_OPS.get(type(node.op))
            if not op:
                raise ValueError(f"허용되지 않은 연산자: {type(node.op)}")
            l, r = _eval_node(node.left), _eval_node(node.right)
            if op == operator.truediv and r == 0:
                return 0.0
            return op(l, r)
        elif isinstance(node, ast.UnaryOp):
            op = SAFE_OPS.get(type(node.op))
            if not op:
                raise ValueError(f"허용되지 않은 단항연산자")
            return op(_eval_node(node.operand))
        elif isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name):
                raise ValueError("함수 호출 형식 오류")
            fname = node.func.id
            safe_funcs = {
                "abs": abs, "max": max, "min": min, "round": round,
                "log":  lambda x: math.log(abs(x) + 1e-9),
                "sqrt": lambda x: math.sqrt(abs(x)),
                "tanh": math.tanh,
                "sign": lambda x: 1.0 if x > 0 else (-1.0 if x < 0 else 0.0),
            }
            if fname not in safe_funcs:
                raise ValueError(f"허용되지 않은 함수: {fname}")
            args = [_eval_node(a) for a in node.args]
            return float(safe_funcs[fname](*args))
        else:
            raise ValueError(f"허용되지 않은 노드: {type(node)}")

    try:
        tree = ast.parse(formula, mode="eval")
        result = _eval_node(tree.body)
        # -1 ~ +1 클리핑
        return max(-1.0, min(1.0, result))
    except Exception as e:
        raise ValueError(f"수식 오류: {e}")


def _validate_indicator_formula(formula: str) -> tuple[bool, str]:
    """
    생성된 지표 수식 안전성 검증
    """
    # 1. 금지 키워드 체크
    banned = ["import","exec","eval","open","os","sys","__",
              "subprocess","socket","http","request"]
    for b in banned:
        if b in formula.lower():
            return False, f"금지 키워드 포함: {b}"

    # 2. AST 파싱 가능한지
    try:
        ast.parse(formula, mode="eval")
    except SyntaxError as e:
        return False, f"문법 오류: {e}"

    # 3. 테스트 변수로 실제 실행
    test_vars = {
        "open":0.0,"high":1.0,"low":-1.0,"close":0.5,"prev_close":0.4,
        "ema9":0.6,"ema13":0.55,"ema21":0.5,"ema50":0.4,"ema200":0.3,
        "atr14":0.02,"atr7":0.015,"std20":0.01,
        "volume":1000.0,"vol_mult":2.5,"obv":5000.0,"vwap":0.5,
        "rsi14":55.0,"macd_line":0.01,"macd_signal":0.008,
        "williams_r":-45.0,"force_index":200.0,
        "upper_bb":0.7,"lower_bb":0.3,"pivot_high":0.9,"pivot_low":0.1,
    }
    try:
        val = _safe_eval(formula, test_vars)
        if not (-1.0 <= val <= 1.0):
            return False, f"결과값 범위 초과: {val}"
    except Exception as e:
        return False, f"실행 오류: {e}"

    return True, "OK"


def phase3_create_indicator(analytics: dict) -> Optional[dict]:
    """
    LLM이 완전히 새로운 지표 수식 창조
    안전성 검증 → 백테스트 → 라이브러리 등록
    """
    log.info("[Phase3] 지표 수식 창조 시작")

    prompt = INDICATOR_CREATE_PROMPT.format(
        analytics=json.dumps(analytics, ensure_ascii=False, indent=2)
    )

    for attempt in range(3):
        raw = call_llm(prompt, "새로운 지표를 창조하세요.", temperature=0.85)
        indicator = safe_parse_json(raw)
        if not indicator or "formula" not in indicator:
            continue

        formula = indicator["formula"].strip()
        log.info(f"  생성된 수식: {formula}")

        # 안전성 검증
        ok, reason = _validate_indicator_formula(formula)
        if not ok:
            log.warning(f"  ❌ 안전성 검증 실패: {reason}")
            continue

        log.info(f"  ✅ 안전성 검증 통과")

        # 백테스트용 전략 래핑
        wrapped_strategy = {
            "name":        f"custom_{indicator['name']}_v1",
            "description": indicator["description"],
            "inspired_by": indicator.get("inspired_by", []),
            "timeframes":  ["5m"],
            "market_bias": "trend_following",
            "entry": {
                "direction": "both",
                "conditions": [
                    {
                        "step": 1,
                        "type": "custom_indicator",
                        "name": indicator["name"],
                        "formula": formula,
                        "buy_threshold":  indicator.get("buy_threshold",  0.6),
                        "sell_threshold": indicator.get("sell_threshold", -0.6),
                    }
                ],
                "confluence_required": 1,
            },
            "exit": {
                "take_profit": {"type": "fixed_ratio", "value": 2.0},
                "stop_loss":   {"type": "fixed", "value": 1.0},
            },
            "filters":        [],
            "risk_reward_min": 2.0,
        }

        # 백테스트
        bt = run_backtest(wrapped_strategy, days=30)
        if not bt:
            log.warning(f"  ❌ 백테스트 실패")
            continue

        log.info(
            f"  백테스트: 거래={bt['total_trades']}  "
            f"승률={bt['win_rate']:.0%}  Sharpe={bt['sharpe']:.2f}"
        )

        if bt["sharpe"] >= 1.0 and bt["win_rate"] >= 0.45:
            indicator["backtest"]   = bt
            indicator["created_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            indicator["phase"]      = 3
            _register_indicator(indicator)
            log.info(f"  🎉 신규 지표 등록: {indicator['name']}")
            return indicator
        else:
            log.warning(
                f"  ❌ 성과 미달 (Sharpe={bt['sharpe']:.2f} 기준≥1.0, "
                f"승률={bt['win_rate']:.0%} 기준≥45%)"
            )

    return None


def _register_indicator(indicator: dict):
    """커스텀 지표 라이브러리에 등록"""
    lib = []
    if INDICATOR_LIB.exists():
        try:
            lib = json.loads(INDICATOR_LIB.read_text())
        except Exception:
            lib = []

    # 중복 이름 제거
    lib = [i for i in lib if i.get("name") != indicator["name"]]
    lib.append(indicator)
    INDICATOR_LIB.write_text(json.dumps(lib, ensure_ascii=False, indent=2))


def load_custom_indicators() -> list:
    """등록된 커스텀 지표 목록 반환"""
    if not INDICATOR_LIB.exists():
        return []
    try:
        return json.loads(INDICATOR_LIB.read_text())
    except Exception:
        return []


def apply_custom_indicator(indicator: dict, candle_vars: dict) -> float:
    """실시간 신호 판단에 커스텀 지표 적용"""
    try:
        return _safe_eval(indicator["formula"], candle_vars)
    except Exception:
        return 0.0


# ══════════════════════════════════════════════════
#  메인 진화 사이클 (scheduler.py에서 호출)
# ══════════════════════════════════════════════════
def run_evolution_cycle(pool, analytics: dict) -> dict:
    """
    Phase 자동 감지 후 해당 단계 진화 실행
    """
    phase  = detect_phase(analytics)
    total  = analytics.get("total_trades", 0)
    result = {"phase": phase, "new_strategies": [], "new_indicators": []}

    log.info(f"{'='*55}")
    log.info(f"🧬 진화 사이클 — Phase {phase} (누적거래 {total}건)")
    log.info(f"{'='*55}")

    # ── Phase 1: 항상 실행 (기법 조합 창조) ──────────
    new_s = phase1_create(pool.count(), analytics)
    for s in new_s:
        pool.add(s)
    result["new_strategies"].extend(new_s)
    log.info(f"  Phase1 신규 전략: {len(new_s)}개")

    # ── Phase 2: 50건+ (파라미터 진화) ───────────────
    if phase >= 2:
        top_strategies = pool.select_top(n=3)
        evolved = phase2_evolve_params(top_strategies, analytics)
        for s in evolved:
            pool.add(s)
        result["new_strategies"].extend(evolved)
        log.info(f"  Phase2 개선 전략: {len(evolved)}개")

    # ── Phase 3: 200건+ (지표 창조) ──────────────────
    if phase >= 3:
        new_indicator = phase3_create_indicator(analytics)
        if new_indicator:
            result["new_indicators"].append(new_indicator)
            # Phase3 지표 기반 전략도 자동 생성
            indicator_strategy = {
                "name":        f"auto_{new_indicator['name']}",
                "description": f"Phase3 자동 생성: {new_indicator['description']}",
                "phase":       3,
                "timeframes":  ["5m"],
                "market_bias": "trend_following",
                "entry": {
                    "direction": "both",
                    "conditions": [{
                        "step": 1,
                        "type": "custom_indicator",
                        "name": new_indicator["name"],
                        "formula": new_indicator["formula"],
                        "buy_threshold":  new_indicator.get("buy_threshold",  0.6),
                        "sell_threshold": new_indicator.get("sell_threshold", -0.6),
                    }],
                    "confluence_required": 1,
                },
                "exit": {
                    "take_profit": {"type": "fixed_ratio", "value": 2.0},
                    "stop_loss":   {"type": "fixed", "value": 1.0},
                },
                "filters":        [],
                "risk_reward_min": 2.0,
                "backtest":       new_indicator.get("backtest", {}),
            }
            pool.add(indicator_strategy)
            result["new_strategies"].append(indicator_strategy)
        log.info(f"  Phase3 신규 지표: {len(result['new_indicators'])}개")

    # ── 하위 전략 폐기 ────────────────────────────────
    killed = pool.kill_underperformers(
        min_trades=10, min_win_rate=0.40, min_sharpe=-0.5
    )
    log.info(f"  폐기: {len(killed)}개 {killed}")

    # ── 결과 저장 ─────────────────────────────────────
    summary_file = DATA_DIR / "evolution_log.json"
    log_data = []
    if summary_file.exists():
        try:
            log_data = json.loads(summary_file.read_text())
        except Exception:
            log_data = []

    log_data.append({
        "time":           time.strftime("%Y-%m-%dT%H:%M:%S"),
        "phase":          phase,
        "total_trades":   total,
        "new_strategies": [s.get("name") for s in result["new_strategies"]],
        "new_indicators": [i.get("name") for i in result["new_indicators"]],
        "killed":         killed,
    })
    log_data = log_data[-60:]  # 최근 60사이클 유지
    summary_file.write_text(json.dumps(log_data, ensure_ascii=False, indent=2))

    log.info(f"✅ 진화 사이클 완료")
    return result
