# StandX 메이커 포인트 파밍 봇

**버전**: v1.3.0
**최종 업데이트**: 2026-01-07

StandX DEX에서 메이커 포인트를 자동으로 파밍하는 봇입니다.

## 주요 특징

- **2+2 전략**: 양방향 각 2개 주문 (6bps + 8bps)
- **교차 순차 재배치**: 업타임 극대화 (Buy1→Sell1→Buy2→Sell2)
- **자동 리밸런싱**: Band 이탈 또는 Drift 15 bps 초과 시
- **자동 청산**: 체결 시 즉시 반대 방향 마켓 오더
- **3단계 안전 체계**: Pre-Kill → Lock → Hard Kill
- **빠른 동기화**: 2초 간격, WebSocket 재연결 10초

## 포인트 구조

| 밴드 | 거리 (bps) | 포인트 비율 |
|------|-----------|------------|
| **Band A** | 0 ~ 10 | 100% |
| Band B | 10 ~ 30 | 50% |
| Band C | 30 ~ 100 | 10% |

> **bps** = 0.01% (예: 8 bps = 0.08%)

## 빠른 시작

### 1. 의존성 설치
```bash
pip install -r requirements.txt
```

### 2. 환경변수 설정
```bash
cp .env.example .env
```

`.env` 파일 편집:
```env
WALLET_ADDRESS=0x지갑주소...
WALLET_PRIVATE_KEY=0x개인키...
```

### 3. 실행
```bash
# 인터랙티브 모드 (권장)
python interactive.py

# 또는 바탕화면 바로가기 사용
```

## 인터랙티브 명령어

| 명령 | 설명 |
|------|------|
| `1` | 주문 크기 설정 |
| `2` | 잔액 확인 |
| `3` | 봇 시작 (RUN) |
| `4` | 봇 정지 (STOP) |
| `5` | 상태 확인 |
| `6` | 설정 확인 |
| `q` | 종료 |

## 설정 (config.yaml)

```yaml
strategy:
  symbols:
    - "BTC-USD"
  order_size_usd: 5           # 주문당 금액 (USD)
  target_distance_bps: 8      # Mark Price로부터 거리
  drift_threshold_bps: 4      # 리밸런싱 트리거
  order_lock_seconds: 0.7     # 주문 Lock 시간
  rebalance_cooldown_seconds: 3  # 쿨다운

safety:
  max_position_usd: 50        # 최대 포지션 한도
  pre_kill:
    volatility_threshold_bps: 15
    mark_mid_divergence_bps: 3
    pause_duration_seconds: 5
  hard_kill:
    max_volatility_bps: 30
    stale_threshold_seconds: 30
```

## 동작 원리 (2+2 전략)

```
Mark Price = $94,000 기준

[매수 주문 1] $93,943.60 (-6 bps)
[매수 주문 2] $93,924.80 (-8 bps)
[매도 주문 1] $94,056.40 (+6 bps)
[매도 주문 2] $94,075.20 (+8 bps)

→ Band A 중심부 배치로 이탈 최소화
→ 양방향 4개 주문으로 업타임 극대화
```

## 안전 장치 (3단계)

### Pre-Kill (사전 예방)
- 변동성 15 bps/초 초과 → 신규 주문 5초 중단
- Mark/Mid 괴리 3 bps 초과 → 신규 주문 5초 중단
- 기존 주문은 유지 (Duration 보존)

### Lock 시스템
- 주문 생성 후 0.7초간 취소 금지
- 0.5초 Duration 조건 충족

### Hard Kill
- 급변 30 bps/초 → 즉시 전체 취소
- 데이터 Stale 30초 → 경고 (취소 안 함)

## 상태 출력 예시

```
================================================================
Running: True
Runtime: 0.50 hours
Emergency Stopped: False
------------------------------------------------------------
Orders Placed: 4
Orders Cancelled: 2
Rebalances: 1
Fills: 0
Liquidations: 0
Estimated Points: 0.4
------------------------------------------------------------

[BTC-USD]
  Mid Price: $93,811.30
  Spread: 0.0 bps
  BUY: 0.0001 @ $93,735.80 (submitted)
  SELL: 0.0001 @ $93,885.90 (submitted)
================================================================
```

## 파일 구조

```
standx_maker_bot/
├── api/
│   ├── auth.py              # ed25519 인증
│   ├── rest_client.py       # REST API
│   └── websocket_client.py  # WebSocket
├── core/
│   ├── band_calculator.py   # Band 계산
│   ├── order_manager.py     # 주문 관리 (v1.2.0 개선)
│   ├── price_tracker.py     # 가격 추적
│   └── safety_guard.py      # 3단계 안전 장치
├── strategy/
│   └── maker_farming.py     # 메인 전략 (v1.2.0 개선)
├── utils/
│   ├── config.py            # 설정 관리
│   └── logger.py            # 로깅
├── config.yaml              # 설정 파일
├── interactive.py           # 인터랙티브 CLI
├── main.py                  # 메인 진입점
├── REPORT.md                # 상세 기술 문서
└── requirements.txt         # 의존성
```

## 트러블슈팅

### 주문이 바로 취소됨
- v1.2.0에서 해결됨
- sync_orders 빈도 최적화 (5초 간격)
- 404 에러 처리 개선 (10초 유예)

### "기준 가격 정보 없음"
- WebSocket 연결 불안정 시 REST API 폴백 자동 적용

### "invalid order qty"
- BTC-USD 최소 수량: 0.0001 (약 $9.70)
- 주문 크기 $10 이상 권장

### HTTP 404 오류
- 정상 동작 (거래소 반영 지연)
- v1.2.0에서 자동 처리됨

## 업데이트 히스토리

### v1.3.0 (2026-01-07)
- **2+2 전략**: 양방향 각 2개 주문 (6bps + 8bps)
- **교차 순차 재배치**: 업타임 극대화
- Drift 임계값 8→15bps
- 동기화 간격 5초→2초
- WebSocket 재연결 60초→10초
- 동적 거리 활성화 (5.5-8.5bps)

### v1.2.0 (2026-01-06)
- 주문 동기화 안정화
- 404 에러 처리 개선
- Hard Kill stale 임계값 완화 (30초)

### v1.1.0 (2026-01-06)
- Pre-Kill 레이어 추가
- Mark/Mid 괴리 감지

### v1.0.0 (2026-01-06)
- 초기 버전

## 주의사항

- **테스트 먼저**: 소액($5)으로 테스트 후 증액
- **개인키 보안**: `.env` 파일 절대 공유 금지
- **리스크**: 급격한 가격 변동 시 체결 가능

## 상세 문서

자세한 기술 문서는 [REPORT.md](REPORT.md) 참조
