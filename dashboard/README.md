# Dashboard (Portable)

이 폴더는 다른 PC로 그대로 복사 가능한 **읽기 전용 미니 대시보드**입니다.

## 실행
```bash
cd dashboard
python3 -m http.server 8787
# 브라우저: http://127.0.0.1:8787
```

## 데이터 소스
- `./data/status.json` 를 읽어 화면 갱신합니다.
- 10초마다 자동 새로고침(fetch)합니다.

## 다른 PC로 옮기기
1. `dashboard/` 폴더 복사
2. Python 3 설치 확인
3. 위 실행 명령으로 바로 열기

## 보안
- API Key/Secret 노출 금지
- 대시보드는 민감정보를 표시하지 않음
