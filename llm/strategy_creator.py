"""
volky-bot / llm / strategy_creator.py

로컬 LLM (Ollama)을 사용한 전략 생성/검증/진화 모듈
"""

import json
import requests
import time
from pathlib import Path
from typing import Optional

# ── 설정 ─────────────────────────────────────────
OLLAMA_URL = "http://localhost:11434/api/generate"
MODEL_NAME = "qwen3.5:35b-a3b"      # 기본 선호 모델
MODEL_FAST = "qwen3.5:9b"           # 기본 빠른 모델
TIMEOUT    = 120                     # 전략 생성은 느려도 됨
TIMEOUT_FAST = 10                    # 신호 판단은 빠르게

PROMPTS_DIR = Path(__file__).parent / "prompts"

def _get_ollama_models(timeout: int = 3) -> set[str]:
    """로컬 Ollama 모델 태그 조회"""
    try:
        base = OLLAMA_URL.split('/api/')[0]
        r = requests.get(f"{base}/api/tags", timeout=timeout)
        r.raise_for_status()
        data = r.json()
        return {m.get('name', '') for m in data.get('models', []) if m.get('name')}
    except Exception:
        return set()

def resolve_model(preferred: str, fallbacks: list[str]) -> str:
    """설치된 모델 태그에 맞춰 자동 선택"""
    available = _get_ollama_models()
    if not available:
        return preferred
    candidates = [preferred] + fallbacks
    for c in candidates:
        if c in available:
            return c
    # 접미사/별칭 완화 매칭
    for c in candidates:
        prefix = c.split(':')[0]
        for a in available:
            if a.startswith(prefix + ':'):
                return a
    return preferred

# ── 프롬프트 로더 ─────────────────────────────────
def load_prompt(name: str) -> str:
    path = PROMPTS_DIR / f"{name}.txt"
    return path.read_text(encoding="utf-8")

# ── Ollama 호출 ───────────────────────────────────
def call_llm(
    system_prompt: str,
    user_message: str,
    model: str = MODEL_NAME,
    timeout: int = TIMEOUT,
    temperature: float = 0.7,
) -> Optional[str]:
    model = resolve_model(model, ["qwen3.5:35b", "qwen3.5:30b", "qwen3.5:14b", "qwen3.5:9b"])
    payload = {
        "model": model,
        "prompt": f"{system_prompt}\n\n---\n\n{user_message}",
        "stream": False,
        "think": False,
        "options": {
            "temperature": temperature,
            "top_p": 0.9,
            "num_predict": 2000,
        }
    }
    try:
        resp = requests.post(OLLAMA_URL, json=payload, timeout=timeout)
        resp.raise_for_status()
        return resp.json().get("response", "").strip()
    except requests.exceptions.Timeout:
        print(f"[LLM] 타임아웃 ({timeout}s)")
        return None
    except Exception as e:
        print(f"[LLM] 오류: {e}")
        return None

# ── JSON 파싱 (환각 방어) ─────────────────────────
def safe_parse_json(text: str) -> Optional[dict]:
    """LLM 응답에서 JSON 추출 (마크다운 코드블록 제거 등)"""
    if not text:
        return None
    # 코드블록 제거
    text = text.replace("```json", "").replace("```", "").strip()
    # JSON 시작점 찾기
    start = text.find("{")
    end   = text.rfind("}") + 1
    if start == -1 or end == 0:
        return None
    try:
        return json.loads(text[start:end])
    except json.JSONDecodeError as e:
        print(f"[JSON] 파싱 실패: {e}")
        return None

# ── 전략 생성 ─────────────────────────────────────
def create_strategy(
    trader_focus: list[str] = None,
    style: str = "any",
    retry: int = 3,
) -> Optional[dict]:
    """
    새 스캘핑 전략 생성

    Args:
        trader_focus: 참고할 트레이더 목록 (None=랜덤 조합)
        style: 'trend_following' | 'mean_reversion' | 'breakout' | 'any'
        retry: 실패 시 재시도 횟수
    """
    system = load_prompt("create_strategy")

    focus_hint = ""
    if trader_focus:
        focus_hint = f"이번에는 특히 {', '.join(trader_focus)} 기법을 중심으로 조합하세요."
    style_hint = ""
    if style != "any":
        style_hint = f"전략 스타일은 반드시 {style} 방식으로 만드세요."

    user_msg = f"""
새로운 BTC/USDT 스캘핑 전략을 만들어주세요.

{focus_hint}
{style_hint}

요구사항:
- 5분봉 또는 15분봉 기반
- 최소 2개 이상의 서로 다른 트레이더 기법 조합
- 기존에 없는 독창적인 조합 우선
- JSON 형식만 출력 (설명 없이)
"""

    for attempt in range(1, retry + 1):
        print(f"[CREATE] 전략 생성 시도 {attempt}/{retry}...")
        raw = call_llm(system, user_msg, temperature=0.8)
        strategy = safe_parse_json(raw)
        if strategy and "name" in strategy and "entry" in strategy:
            print(f"[CREATE] ✅ '{strategy['name']}' 생성 완료")
            return strategy
        print(f"[CREATE] ⚠️ 파싱 실패, 재시도...")
        time.sleep(2)

    print("[CREATE] ❌ 전략 생성 실패")
    return None

# ── 전략 검증 ─────────────────────────────────────
def validate_strategy(strategy: dict) -> Optional[dict]:
    """생성된 전략의 논리 검증"""
    system = load_prompt("validate_strategy")
    user_msg = f"다음 전략을 검증해주세요:\n\n{json.dumps(strategy, ensure_ascii=False, indent=2)}"

    raw = call_llm(system, user_msg, temperature=0.1)  # 검증은 낮은 temperature
    result = safe_parse_json(raw)
    if result:
        verdict = result.get("verdict", "UNKNOWN")
        score   = result.get("score", 0)
        print(f"[VALIDATE] {verdict} (점수: {score}/100)")
    return result

# ── 실시간 신호 판단 ──────────────────────────────
def judge_signal(strategy: dict, market_data: dict) -> Optional[dict]:
    """실시간 진입 신호 판단 (빠른 모델 사용)"""
    system = load_prompt("signal_judge")
    user_msg = json.dumps({
        "strategy": strategy,
        "market": market_data
    }, ensure_ascii=False)

    raw = call_llm(
        system, user_msg,
        model=resolve_model(MODEL_FAST, ["qwen3.5:9b", "qwen3.5:14b", "qwen3.5:35b", "qwen3.5:30b"]),
        timeout=TIMEOUT_FAST,
        temperature=0.1
    )
    return safe_parse_json(raw)

# ── 전략 진화 ─────────────────────────────────────
def evolve_strategy(
    strategies: list[dict],
    performance: list[dict],
    mode: str = "mutate",
) -> Optional[dict]:
    """전략 돌연변이 또는 교배"""
    system = load_prompt("evolve_strategy")
    user_msg = json.dumps({
        "mode": mode,
        "strategies": strategies,
        "performance": performance
    }, ensure_ascii=False, indent=2)

    raw = call_llm(system, user_msg, temperature=0.6)
    result = safe_parse_json(raw)
    if result and mode in ("mutate", "crossover"):
        print(f"[EVOLVE] 새 전략 '{result.get('name')}' 생성")
    return result

# ── 전략 분석 ─────────────────────────────────────
def analyze_strategy(strategy: dict, performance: dict) -> Optional[dict]:
    """전략 성과 분석 및 개선 방향 도출"""
    return evolve_strategy([strategy], [performance], mode="analyze")


# ── 테스트 ────────────────────────────────────────
if __name__ == "__main__":
    print("=== VOLKY Strategy Creator 테스트 ===\n")

    # 1. 전략 생성
    strategy = create_strategy(
        trader_focus=["ICT", "Wyckoff"],
        style="mean_reversion"
    )
    if not strategy:
        print("전략 생성 실패")
        exit(1)

    print("\n생성된 전략:")
    print(json.dumps(strategy, ensure_ascii=False, indent=2))

    # 2. 전략 검증
    print("\n=== 검증 중... ===")
    validation = validate_strategy(strategy)
    if validation:
        print(json.dumps(validation, ensure_ascii=False, indent=2))

    # 3. 결과 저장
    if validation and validation.get("verdict") == "APPROVED":
        out = Path("data/strategy_pool.json")
        out.parent.mkdir(exist_ok=True)
        pool = []
        if out.exists():
            pool = json.loads(out.read_text())
        strategy["validation"] = validation
        strategy["created_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        pool.append(strategy)
        out.write_text(json.dumps(pool, ensure_ascii=False, indent=2))
        print(f"\n✅ 전략 풀에 저장 완료 (총 {len(pool)}개)")
