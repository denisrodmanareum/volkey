# VOLKY BOT

세계 유명 트레이더 기법을 LLM이 조합/창조하는 급등 스캘핑 전용 봇

## 아키텍처

```
[Qwen3.5-35B-A3B]  전략 생성/검증/진화 (Ollama 로컬)
[Qwen3.5-9B]       실시간 신호 판단
       ↓
[SurgeScalper]     급등 감지 → 패턴분류 → 펌프필터 → 진입판단
[Scheduler]        03:00 KST 정규 커스텀 + 긴급 트리거
[StrategyPool]     전략 관리/점수화/폐기
[Backtester]       Binance 과거데이터 룰 기반 검증
       ↓
[Dashboard]        GitHub Pages 실시간 모니터링
```

## 파일 구조

```
volky-bot/
├── llm/
│   ├── strategy_creator.py       # LLM 호출 모듈
│   └── prompts/
│       ├── create_strategy.txt   # 전략 생성 프롬프트
│       ├── validate_strategy.txt # 전략 검증 프롬프트
│       ├── signal_judge.txt      # 실시간 신호 판단
│       └── evolve_strategy.txt   # 전략 진화/교배
├── engine/
│   ├── surge_scalper.py          # 급등 스캘핑 통합 엔진
│   ├── scheduler.py              # 하이브리드 스케줄러
│   ├── strategy_pool.py          # 전략 풀 관리
│   └── backtester.py             # 백테스트 엔진
├── dashboard/
│   └── index.html                # GitHub Pages 대시보드
├── data/                         # 런타임 데이터 (자동 생성)
│   ├── status.json               # 봇 상태 (대시보드 연동)
│   ├── surge_status.json         # 급등 스캐너 결과
│   ├── strategy_pool.json        # 전략 풀
│   └── surge_analytics.json     # 사후 분석 통계
└── requirements.txt
```

## 설치 및 실행

```bash
# 1. 패키지 설치
pip install -r requirements.txt

# 2. Ollama 모델 설치
ollama pull qwen3.5:35b-a3b   # 전략 생성용
ollama pull qwen3.5:9b         # 신호 판단용

# 3. 급등 스캐너 실행
python engine/surge_scalper.py

# 4. 스케줄러 실행 (별도 터미널)
python engine/scheduler.py
```

## 스케줄

| 시각 | 작업 |
|------|------|
| 매일 03:00 KST | LLM 전략 생성/검증/백테스트/등록 |
| 매주 일요일 02:00 KST | 전략 진화 (교배+돌연변이) |
| 5분마다 | 긴급 트리거 감시 |

## 급등 패턴 분류

| 패턴 | 설명 | 진입 여부 |
|------|------|---------|
| 🟢 축적형 | 횡보 후 거래량 동반 돌파 | ✅ 최우선 |
| 🔵 연속형 | 추세 중 눌림 후 재급등 | ✅ 좋음 |
| 🟡 뉴스형 | 거래량 폭발, 이유 있음 | ⚠️ 주의 |
| 🔴 펌프형 | 이유 불명 급등 | ❌ 금지 |

## 긴급 트리거

- 연속 손실 3회 → 전략 비활성화
- 승률 40% 이하 → 백업 전략 교체  
- 낙폭 5% 초과 → 거래 중단
- 거래량 4x 급변 → 백업 전략 교체

## 모델 설정

```python
# llm/strategy_creator.py
MODEL_NAME = "qwen3.5:35b-a3b"   # 전략 생성 (품질 우선)
MODEL_FAST = "qwen3.5:9b"        # 신호 판단 (속도 우선)
```

## 대시보드 배포

```bash
# coin 레포에 dashboard/index.html 교체 후 push
# GitHub Pages 자동 배포
# surge_status.json은 sync_status_and_push.sh로 동기화
```
