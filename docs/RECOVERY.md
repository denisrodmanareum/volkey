# RECOVERY.md - 호출/게이트웨이 재발방지 & 복구 가이드

## 목적
호출 끊김(응답 없음), 10분 리포트 누락, 게이트웨이/런처 경로 문제를 빠르게 감지/복구하고 재발을 방지한다.

## 재발방지 기본 규칙

1. **절대경로 고정**
   - `python`, `node`, `openclaw` 실행은 PATH 의존 금지
   - launchd/스크립트 모두 절대경로 사용

2. **실행 전 헬스체크**
   - 경로 존재 확인(`-x`)
   - 필수 파일 존재 확인(`config/.env`, 스크립트 경로)

3. **10분 리포트 실패 감지**
   - 연속 1회 실패 시 즉시 경고 전송
   - 연속 2회 실패 시 자동 복구 루틴 수행

4. **게이트웨이 상태 점검 자동화**
   - 비정상 감지 시 `openclaw gateway restart`
   - 복구 후 테스트 전송 1회

5. **동기화 기준 통일**
   - 포지션 정보는 `positionRisk`를 단일 진실 소스로 사용

---

## 빠른 점검 명령

```bash
# 1) 게이트웨이 상태
openclaw gateway status

# 2) 10분 리포트 런처 상태
launchctl print gui/$(id -u)/com.angelareum.volky.report10m | head -60

# 3) 리포트 에러 로그
tail -n 80 ~/Desktop/2026/volky-bot/logs/report10m.err.log

# 4) 최근 리포트 출력 로그
tail -n 80 ~/Desktop/2026/volky-bot/logs/10m-report.log
```

---

## 자동복구 절차 (우선순위)

### A. 리포트만 누락될 때
1) 런처 재기동
```bash
launchctl kickstart -k gui/$(id -u)/com.angelareum.volky.report10m
```
2) 수동 1회 전송 테스트
```bash
/Users/riot91naver.com/Desktop/2026/venv-chronos311/bin/python /Users/riot91naver.com/Desktop/2026/volky-bot/scripts/send_10m_report.py
```

### B. 게이트웨이 응답 이상일 때
1) 상태 확인
```bash
openclaw gateway status
```
2) 재시작
```bash
openclaw gateway restart
```
3) 테스트 메시지 1회
```bash
openclaw message send --channel telegram --target 1463388329 --message "[복구확인] gateway/reporter 정상"
```

### C. 봇 루프 이상일 때
1) 프로세스 확인
```bash
pgrep -fl 'scalp_live_testnet.py'
```
2) 재시작
```bash
pkill -f 'scalp_live_testnet.py' || true
cd ~/Desktop/2026/volky-bot
nohup /Users/riot91naver.com/Desktop/2026/venv-chronos311/bin/python scripts/scalp_live_testnet.py >> logs/scalp-live.out.log 2>> logs/scalp-live.err.log &
```

---

## 운영 체크리스트 (매일)
- [ ] 게이트웨이 상태 정상
- [ ] 10분 리포트 최근 1회 이상 수신
- [ ] 리포트 에러 로그 치명 오류 없음
- [ ] 봇 프로세스 실행 중
- [ ] 포지션 동기화 정상(앱과 리포트 일치)

---

## 비상 원칙
- 복구 실패가 2회 이상 반복되면 신규 진입 중지
- 원인 미확정 상태에서 리스크 확대 금지
- 항상 Testnet에서 먼저 복구 검증 후 운영 유지
