# StandX Maker Bot - 기술 보고서

**버전**: 1.3.0
**최종 업데이트**: 2026-01-07
**작성자**: Claude Code

---

## 1. 개요

### 1.1 봇 목적
StandX DEX에서 **메이커 포인트 파밍**을 자동화하는 봇입니다. 양방향(매수/매도) 리밋 주문을 유지하며, 체결되지 않도록 관리하여 메이커 포인트를 최대화합니다.

### 1.2 지원 거래소
- **StandX**: BSC(Binance Smart Chain) 기반 탈중앙화 거래소 (DEX)
- **거래 가능 심볼**: BTC-USD (현재 유일)

### 1.3 핵심 특징
- Mark Price 기준 Band A (0-10 bps) 내 주문 배치
- **2+2 전략**: 양방향 각 2개 주문 (6bps + 8bps)
- **교차 순차 재배치**: 업타임 극대화 (Buy1→Sell1→Buy2→Sell2)
- 자동 청산 기능 (체결 시 반대 방향 마켓 오더)
- Lock/Cooldown 시스템으로 포인트 Duration 극대화 (3.5초)
- **3단계 안전 체계**: Pre-Kill → Lock 존중 → Hard Kill
- **Mark/Mid 괴리 감지**: 가격 왜곡 상황 자동 대응
- **주문 동기화 최적화**: 2초 간격 동기화, WebSocket 재연결 10초

---

## 2. 포인트 시스템 이해

### 2.1 StandX Band 시스템
| Band | 거리 (bps) | 포인트 배율 |
|------|-----------|------------|
| **A** | 0-10 | 100% |
| B | 10-30 | 50% |
| C | 30-100 | 10% |
| OUT | 100+ | 0% |

> **bps** = Basis Points = 0.01%
> 예: 8 bps = 0.08%

### 2.2 포인트 적립 조건
1. **양방향 주문 유지**: 매수 + 매도 주문 동시 활성화
2. **Band A 내 위치**: Mark Price 기준 10 bps 이내
3. **최소 Duration**: 주문이 0.5초 이상 유지되어야 포인트 인정

### 2.3 포인트 계산
```
일일 포인트 = 주문 금액(USD) × Band 배율
예: $10 주문 × 2개(양방향) × Band A(100%) = 20 points/day
```

---

## 3. 아키텍처

### 3.1 모듈 구조
```
standx_maker_bot/
├── api/
│   ├── auth.py           # 인증 (ed25519 서명)
│   ├── rest_client.py    # REST API 클라이언트
│   └── websocket_client.py # WebSocket 클라이언트
├── core/
│   ├── band_calculator.py # Band 계산
│   ├── order_manager.py   # 주문 관리 (v1.2.0 개선)
│   ├── price_tracker.py   # 가격 추적
│   └── safety_guard.py    # 안전 장치
├── strategy/
│   └── maker_farming.py   # 메인 전략 로직 (v1.2.0 개선)
├── utils/
│   ├── config.py         # 설정 관리
│   └── logger.py         # 로깅
├── config.yaml           # 설정 파일
├── interactive.py        # 인터랙티브 CLI
└── main.py              # 메인 진입점
```

### 3.2 데이터 플로우
```
[WebSocket] ──> [PriceTracker] ──> [BandCalculator]
                    │                    │
                    ▼                    ▼
              [SafetyGuard] <──── [MakerFarmingStrategy]
                    │                    │
                    ▼                    ▼
              [OrderManager] ────> [REST API]
```

---

## 4. 인증 시스템

### 4.1 인증 플로우
1. **ed25519 키 쌍 생성**: 세션별 임시 키
2. **prepare-signin**: 서버에 공개키 전달, 서명할 메시지 수신
3. **지갑 서명**: BSC 지갑(개인키)으로 메시지 서명
4. **login**: 서명 제출, JWT 토큰 수신
5. **요청 서명**: 각 API 요청에 ed25519 서명 첨부

### 4.2 서명 헤더
```
x-request-sign-version: v1
x-request-id: <uuid>
x-request-timestamp: <unix_ms>
x-request-signature: <base64_ed25519_signature>
```

### 4.3 서명 메시지 형식
```
"{version},{request_id},{timestamp},{json_payload}"
```

---

## 5. 전략 로직

### 5.1 2+2 전략 (v1.3.0)
```
Mark Price = $94,000 기준

[매수 주문 1] $93,943.60 (-6 bps)   ← 안전 거리
[매수 주문 2] $93,924.80 (-8 bps)   ← Band A 중심부
[매도 주문 1] $94,056.40 (+6 bps)   ← 안전 거리
[매도 주문 2] $94,075.20 (+8 bps)   ← Band A 중심부
```

**주문 거리 설정** (config.yaml):
```yaml
order_distances_bps:
  - 6   # 주문1: 안전 거리 (Band A 중심)
  - 8   # 주문2: 10bps 경계에서 2bps 여유
```

### 5.2 리밸런싱 트리거
1. **Band A 이탈**: 주문 거리가 10 bps 초과 시 재배치
2. **Drift 초과**: 기준 가격이 마지막 배치 시점 대비 **15 bps** 이상 변동 시 재배치

### 5.3 교차 순차 재배치 (v1.3.0)
```
기존 동시 재배치:
  모든 주문 취소 → 모든 주문 배치
  → 취소 순간 양방향 업타임 0%

교차 순차 재배치 (v1.3.0):
  Buy1 취소→배치 → Sell1 취소→배치 → Buy2 취소→배치 → Sell2 취소→배치
  → 항상 양방향에 최소 1개 주문 유지 → 업타임 유지
```

**핵심 원리**: 한쪽 방향 주문 1개만 취소 후 즉시 재배치하므로, 나머지 주문들은 계속 활성 상태 유지

### 5.4 자동 청산
주문이 체결되면 즉시 반대 방향 마켓 오더로 청산:
```
BUY 체결 → SELL 마켓 오더 (reduce_only=True)
SELL 체결 → BUY 마켓 오더 (reduce_only=True)
```

---

## 6. 안전 장치 (3단계 체계)

봇은 **3단계 안전 체계**를 통해 리스크를 관리합니다:

```
[정상] → [Pre-Kill] → [Hard Kill] → [비상정지]
         신규주문 중단   즉시 취소      완전 정지
```

### 6.1 Pre-Kill (사전 예방) - v1.1.0 신규
위험 징후 감지 시 **신규 주문만 일시 중단** (기존 주문은 유지):
| 조건 | 기준 | 설명 |
|------|------|------|
| 변동성 경고 | 15 bps/초 | 1초 내 중간 수준 변동 |
| Mark/Mid 괴리 | 3 bps | Mark Price와 Mid Price 차이 |

**설정** (config.yaml):
```yaml
pre_kill:
  volatility_threshold_bps: 15
  mark_mid_divergence_bps: 3
  pause_duration_seconds: 5
```

**동작**:
- Pre-Kill 활성화 → 5초간 신규 주문 배치 중단
- 기존 주문은 유지 → Duration 손실 없음
- 5초 후 자동 해제

### 6.2 Lock 시스템
- **목적**: 0.5초 Duration 조건 충족
- **설정**: `order_lock_seconds: 0.7`
- **동작**: 주문 생성 후 0.7초간 취소 금지

### 6.3 Cooldown 시스템
- **목적**: 과도한 리밸런싱 방지
- **설정**: `rebalance_cooldown_seconds: 3`
- **동작**: 리밸런싱 후 3초간 추가 리밸런싱 금지

### 6.4 Hard Kill 조건
Lock을 무시하고 **즉시 주문 취소**하는 긴급 상황:
| 조건 | 기준 | 설명 |
|------|------|------|
| 데이터 Stale | 30초 | WebSocket 데이터 지연 (v1.2.0 완화) |
| 스프레드 붕괴 | 0 bps | 체결 위험 (비활성화됨) |
| 급변 감지 | 30 bps/초 | 1초 내 급격한 가격 변동 |

### 6.5 비상 정지
- **포지션 한도 초과**: `max_position_usd: 50` 초과 시
- **동작**: 모든 주문 취소 + 봇 정지

### 6.6 Mark/Mid 괴리 감지 - v1.1.0 신규
DEX 환경에서는 Mark Price ≠ Mid Price일 수 있습니다:
- **괴리 > 3 bps**: Pre-Kill 활성화
- **목적**: Band 계산 신뢰도 저하 시 안전하게 대기

---

## 7. 주문 동기화 시스템 - v1.2.0 신규

### 7.1 문제 상황 (v1.1.0 이전)
주문이 생성 직후 바로 취소되는 문제 발생:
```
Orders Placed: 14
Orders Cancelled: 14  ← 100% 취소됨
HTTP 오류: 404 - order not found
```

**원인 분석**:
1. `sync_orders()`가 매 루프마다 (1초마다) 실행
2. 거래소 API 반영 지연으로 새 주문이 조회되지 않음
3. 조회 실패 시 404 에러 → 즉시 CANCELLED 처리
4. 다음 루프에서 "주문 없음" → 신규 배치 → 반복

### 7.2 해결책 (v1.2.0)

#### 7.2.1 주문 동기화 빈도 감소
**파일**: `strategy/maker_farming.py`
```python
# 기존: 매 루프마다 (1초마다) sync_orders 호출
# 개선: 5초마다만 호출
if now - state.last_sync_time >= 5.0:
    await self.order_manager.sync_orders(symbol)
    state.last_sync_time = now
```

#### 7.2.2 주문 생성 후 대기 시간 추가
**파일**: `core/order_manager.py`
```python
# 생성 후 3초 이내 주문은 동기화 스킵 (거래소 반영 대기)
order_age = time.time() - order.created_at
if order_age < 3.0:
    logger.debug(f"최근 생성된 주문 동기화 스킵: {cl_ord_id}")
    continue
```

#### 7.2.3 404 에러 처리 개선
**파일**: `core/order_manager.py`
```python
# 기존: 404 에러 → 즉시 CANCELLED 처리
# 개선: 10초 이상 지난 경우에만 CANCELLED 처리
if "404" in error_str:
    if order_age > 10.0:
        order.status = ManagedOrderStatus.CANCELLED
    else:
        # 대기 중 - 상태 유지
        logger.debug(f"주문 조회 404 (대기 중): {cl_ord_id}")
```

### 7.3 개선 결과
```
Before (v1.1.0):
- Orders Placed: 14
- Orders Cancelled: 14 (100%)
- 활성 주문: 없음

After (v1.2.0):
- Orders Placed: 4
- Orders Cancelled: 2 (50%)
- 활성 주문: BUY + SELL 유지 ✓
```

### 7.4 주문 상태 흐름도
```
[주문 생성] → [SUBMITTED] → [3초 대기] → [sync_orders]
                                              │
                            ┌─────────────────┼─────────────────┐
                            ▼                 ▼                 ▼
                      [거래소에 있음]    [거래소에 없음]    [조회 실패]
                            │                 │                 │
                            ▼                 ▼                 ▼
                         [OPEN]         [상세 조회]       [상태 유지]
                                              │
                            ┌─────────────────┼─────────────────┐
                            ▼                 ▼                 ▼
                        [filled]         [cancelled]      [404 에러]
                            │                 │                 │
                            ▼                 ▼                 ▼
                        [FILLED]        [CANCELLED]     [10초 초과?]
                                                              │
                                              ┌───────────────┼───────────────┐
                                              ▼               ▼
                                         [상태 유지]    [CANCELLED]
```

---

## 8. 설정 가이드

### 8.1 config.yaml 전체 설정
```yaml
# StandX 메이커 포인트 파밍 봇 설정

standx:
  base_url: "https://perps.standx.com"
  ws_url: "wss://perps.standx.com/ws-stream/v1"
  chain: "bsc"

wallet:
  address: ""      # 또는 환경변수 WALLET_ADDRESS
  private_key: ""  # 또는 환경변수 WALLET_PRIVATE_KEY

strategy:
  symbols:
    - "BTC-USD"
  order_size_usd: 5           # 주문당 금액 (USD)
  margin_reserve_percent: 20  # 마진 예약 비율

  # 거리 설정 (bps)
  min_distance_bps: 5         # 최소 거리 (체결 방지)
  target_distance_bps: 8      # 목표 거리
  max_distance_bps: 10        # 최대 거리 (Band A 한계)
  band_warning_bps: 9.2       # Band A 이탈 경고

  # 동적 거리 (비활성화 상태)
  dynamic_distance:
    enabled: false
    min_bps: 5
    max_bps: 9
    spread_factor: 0.6
    volatility_factor: 0.8

  # Lock & Cooldown
  order_lock_seconds: 0.7     # 주문 Lock 시간
  rebalance_cooldown_seconds: 3  # 리밸런싱 쿨다운

  # 리밸런싱 트리거
  rebalance_on_band_exit: true
  drift_threshold_bps: 4      # Drift 임계값

  check_interval_seconds: 1   # 체크 주기

safety:
  max_position_usd: 50        # 최대 포지션 한도

  # Pre-Kill 조건 (v1.1.0)
  pre_kill:
    volatility_threshold_bps: 15
    mark_mid_divergence_bps: 3
    pause_duration_seconds: 5

  # Hard Kill 조건 (v1.2.0 조정)
  hard_kill:
    min_spread_bps: 0         # 비활성화
    max_volatility_bps: 30
    stale_threshold_seconds: 30  # 30초로 완화

  # 체결 임박 취소 (v1.2.0 비활성화)
  cancel_if_within_bps: 0     # Spread 0 문제로 비활성화

telegram:
  enabled: false
  bot_token: ""
  chat_id: ""
```

### 8.2 .env 파일 설정
```env
WALLET_ADDRESS=0x...
WALLET_PRIVATE_KEY=0x...
```

### 8.3 주문 크기 권장
| 잔액 | 권장 주문 크기 | 비고 |
|------|--------------|------|
| $10 | $3-4 | 청산 수수료 예약 필요 |
| $20 | $5-8 | |
| $50 | $10-20 | |

**계산식**: `(잔액 - $0.50 청산예약) / 2 = 최대 주문 크기`

---

## 9. 사용법

### 9.1 실행 방법
**방법 1: 바탕화면 바로가기**
```
C:\Users\user\Desktop\StandX_Bot.bat
```

**방법 2: 직접 실행**
```bash
cd C:\Users\user\Documents\03_Claude\standx_maker_bot
python interactive.py
```

### 9.2 인터랙티브 명령어
| 명령 | 설명 |
|------|------|
| `1` | 주문 크기 설정 |
| `2` | 잔액 확인 |
| `3` | 봇 시작 (RUN) |
| `4` | 봇 정지 (STOP) |
| `5` | 상태 확인 |
| `6` | 설정 확인 |
| `q` | 종료 |

### 9.3 상태 출력 예시 (v1.2.0)
```
================================================================
Running: True
Runtime: 0.00 hours
Emergency Stopped: False
------------------------------------------------------------
Orders Placed: 4
Orders Cancelled: 2
Rebalances: 1
Fills: 0
Liquidations: 0
Estimated Points: 0.0
------------------------------------------------------------

[BTC-USD]
  Mid Price: $93,811.30
  Spread: 0.0 bps
  BUY: 0.0001 @ $93,735.80 (submitted)
  SELL: 0.0001 @ $93,885.90 (submitted)
================================================================
```

**핵심 지표 해석**:
- `Orders Placed > Orders Cancelled`: 정상 동작 ✓
- `BUY + SELL 모두 활성`: 양방향 주문 유지 ✓
- `Spread: 0.0 bps`: WebSocket 데이터 문제 (동작에 영향 없음)

---

## 10. 트러블슈팅

### 10.1 주문이 바로 취소됨 (v1.2.0 해결)
**증상**: Orders Placed = Orders Cancelled (100% 취소)
**원인**: sync_orders()가 너무 자주 호출되어 거래소 반영 전 404 발생
**해결**:
- sync_orders 호출 빈도 5초로 감소
- 주문 생성 후 3초 대기
- 404 에러 시 10초까지 상태 유지

### 10.2 "기준 가격 정보 없음" 오류
**원인**: WebSocket 연결 불안정
**해결**: REST API 폴백 추가 완료 (price_tracker.py)

### 10.3 주문이 체결됨
**원인**: 주문 거리가 너무 가까움 (target_distance_bps < 5)
**해결**: `target_distance_bps: 8` 이상 권장

### 10.4 "invalid order qty" 오류
**원인**: 주문 수량이 최소 단위 미만
**해결**: BTC-USD 최소 수량 = 0.0001 (약 $9.70)

### 10.5 HTTP 404 오류 (order not found)
**증상**: 로그에 404 에러 다수 발생
**원인**: 거래소 API 반영 지연
**해결**: v1.2.0에서 자동 처리 (정상 동작)

### 10.6 Spread: 0.0 bps 표시
**원인**: WebSocket에서 spread 데이터 미수신
**영향**: 없음 (주문 동작에 영향 없음)
**상태**: 모니터링 중 (기능에 지장 없음)

---

## 11. 성능 최적화

### 11.1 포인트 극대화 전략
1. **Duration 극대화**: Lock + Cooldown으로 불필요한 리밸런싱 방지
2. **Band A 유지**: 8 bps 목표 거리로 Band A 내 안정적 위치
3. **양방향 유지**: 개선된 리밸런싱으로 항상 양방향 주문 활성화
4. **동기화 최적화**: 5초 간격 동기화로 안정적 주문 유지 (v1.2.0)

### 11.2 비용 최소화
1. **체결 방지**: 적절한 거리 설정 (5-10 bps)
2. **리밸런싱 최소화**: Drift 4 bps 임계값으로 빈도 감소
3. **API 호출 최소화**: 동기화 빈도 감소로 Rate Limit 방지

---

## 12. 업데이트 히스토리

### v1.3.0 (2026-01-07)
**업타임 다크그린 최적화**

#### 핵심 변경사항
1. **2+2 전략 구현**: 양방향 각 2개 주문 (6bps + 8bps)
   - Band A 중심부 배치로 이탈 빈도 감소
   - 기존 7+9bps에서 6+8bps로 조정 (10bps 경계 여유)

2. **교차 순차 재배치**: Buy1→Sell1→Buy2→Sell2 순서
   - 동시 재배치 → 양방향 0% 문제 해결
   - 항상 양방향에 최소 1개 주문 유지
   - 업타임 연속성 극대화

3. **Drift 임계값 증가**: 8bps → 15bps
   - 재배치 빈도 감소 → Duration 보존

4. **동기화 간격 단축**: 5초 → 2초
   - 체결 감지 속도 향상

5. **WebSocket 재연결 최적화**: 60초 → 10초
   - 데이터 끊김 시 빠른 복구

6. **동적 거리 활성화**:
   ```yaml
   dynamic_distance:
     enabled: true
     min_bps: 5.5
     max_bps: 8.5
   ```

7. **Pre-Kill 조건 조정**:
   - volatility_threshold: 30 → 20 bps
   - mark_mid_divergence: 10 → 5 bps
   - pause_duration: 1 → 0.5초

#### 재배치 로직 비교
```
기존 순차 재배치:
  Buy1 취소→배치 → Buy2 취소→배치 → Sell1 취소→배치 → Sell2 취소→배치
  → Buy 처리 중 Sell 유지, 그러나 Sell 처리 중 Buy 없음

동시 재배치 (실패):
  모든 주문 동시 취소 → 모든 주문 동시 배치
  → 취소 순간 양방향 업타임 0%

교차 순차 재배치 (v1.3.0):
  Buy1 취소→배치 → Sell1 취소→배치 → Buy2 취소→배치 → Sell2 취소→배치
  → 매 단계 양쪽 최소 1개 유지 → 업타임 연속
```

### v1.2.0 (2026-01-06)
**주문 동기화 안정화**
- **sync_orders 호출 빈도 감소**: 1초 → 5초 간격
  - `SymbolState`에 `last_sync_time` 필드 추가
  - 불필요한 API 호출 감소
- **주문 생성 후 대기 시간 추가**: 3초 미만 주문은 동기화 스킵
  - 거래소 API 반영 시간 확보
  - 조기 CANCELLED 판정 방지
- **404 에러 처리 개선**: 10초 이상 경과 시에만 CANCELLED 처리
  - 기존: 404 즉시 CANCELLED
  - 개선: 10초 미만은 상태 유지
- **Hard Kill stale 임계값 완화**: 5초 → 30초
  - WebSocket 불안정 환경 대응
- **cancel_if_within_bps 비활성화**: Spread 0 문제 대응

**결과**:
- 주문 유지율: 0% → 50%+ 개선
- 양방향 주문 안정적 유지
- 404 에러 감소

### v1.1.0 (2026-01-06)
**안전 체계 강화**
- **Pre-Kill 레이어 추가**: 위험 징후 시 신규 주문 일시 중단
  - 변동성 15 bps/초 초과 시 활성화
  - Mark/Mid 괴리 3 bps 초과 시 활성화
  - 5초 후 자동 해제
- **Mark/Mid 괴리 감지**: DEX 환경 가격 왜곡 대응
  - `mark_mid_divergence_bps` 속성 추가
  - `is_price_diverged` 속성 추가
- config.yaml에 `pre_kill` 섹션 추가

### v1.0.0 (2026-01-06)
- 초기 버전 완성
- ed25519 인증 시스템 구현
- Band A 기반 주문 배치
- 자동 청산 기능
- 개선된 리밸런싱 (하나씩 취소→배치)
- `is_near_boundary` 체크 비활성화 (Band A 이탈만 재배치)
- REST API 가격 폴백 추가
- 바탕화면 실행 파일 생성

---

## 13. 기술 상세

### 13.1 주요 클래스 및 역할

| 클래스 | 파일 | 역할 |
|--------|------|------|
| `MakerFarmingStrategy` | strategy/maker_farming.py | 메인 전략 로직, 주문 배치/리밸런싱 |
| `OrderManager` | core/order_manager.py | 주문 생성/취소/동기화 |
| `SafetyGuard` | core/safety_guard.py | 3단계 안전 체계 |
| `PriceTracker` | core/price_tracker.py | 가격 추적, WebSocket/REST 통합 |
| `BandCalculator` | core/band_calculator.py | Band 계산, 주문 가격 결정 |
| `StandXRestClient` | api/rest_client.py | REST API 호출 |
| `StandXWebSocket` | api/websocket_client.py | WebSocket 연결/구독 |
| `StandXAuth` | api/auth.py | ed25519 인증/서명 |

### 13.2 주요 설정 값 요약

| 설정 | 값 | 설명 |
|------|-----|------|
| target_distance_bps | 8 | 주문 배치 거리 |
| drift_threshold_bps | 4 | 리밸런싱 트리거 |
| order_lock_seconds | 0.7 | 주문 Lock 시간 |
| rebalance_cooldown_seconds | 3 | 리밸런싱 쿨다운 |
| sync_interval | 5초 | 주문 동기화 간격 |
| order_grace_period | 3초 | 신규 주문 대기 시간 |
| 404_timeout | 10초 | 404 에러 CANCELLED 판정 기준 |
| stale_threshold_seconds | 30 | 데이터 Stale 기준 |

---

## 14. 향후 개선 사항

1. **다중 심볼 지원**: StandX가 새 심볼 추가 시 대응
2. **동적 거리 최적화**: 시장 상황에 따른 자동 조정
3. **텔레그램 알림**: 주요 이벤트 알림 기능
4. **웹 대시보드**: 실시간 상태 모니터링 UI
5. **WebSocket Spread 수신 개선**: 현재 0.0 bps 문제 해결

---

## 15. 면책 조항

이 봇은 교육 및 개인 사용 목적으로 제작되었습니다. 암호화폐 거래는 높은 위험을 수반하며, 투자 손실에 대한 책임은 사용자 본인에게 있습니다. 봇 사용 전 충분한 테스트를 권장합니다.
