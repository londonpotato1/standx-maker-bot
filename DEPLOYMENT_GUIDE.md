# StandX Maker Bot 배포 가이드

## 개요
이 봇은 StandX에서 메이커 포인트를 파밍하는 자동화 봇입니다.
Railway 클라우드에 배포하여 24/7 운영할 수 있습니다.

---

## 1. 사전 준비

### 필요한 것
1. **GitHub 계정** - https://github.com
2. **Railway 계정** - https://railway.app (GitHub로 가입)
3. **StandX 지갑**
   - BSC 지갑 주소
   - 개인키 (Private Key)
4. **텔레그램 봇** (선택사항)
   - Bot Token
   - Chat ID

### StandX 지갑 준비
1. MetaMask 등에서 BSC 네트워크 지갑 생성
2. StandX (https://perps.standx.com)에 지갑 연결
3. USDT 입금 (최소 $50 권장)

### 텔레그램 봇 만들기 (선택)
1. Telegram에서 @BotFather 검색
2. `/newbot` 명령으로 봇 생성
3. Bot Token 저장 (예: `123456789:ABCdefGHI...`)
4. 생성된 봇에게 아무 메시지 전송
5. `https://api.telegram.org/bot<TOKEN>/getUpdates` 접속하여 Chat ID 확인

---

## 2. GitHub 저장소 Fork

1. 원본 저장소 방문: `https://github.com/londonpotato1/standx-maker-bot`
2. 우측 상단 **Fork** 버튼 클릭
3. 본인 계정으로 저장소 복사됨

---

## 3. Railway 배포

### 3.1 프로젝트 생성
1. https://railway.app 접속 후 GitHub 로그인
2. **New Project** 클릭
3. **Deploy from GitHub repo** 선택
4. Fork한 `standx-maker-bot` 저장소 선택

### 3.2 환경변수 설정 (중요!)
Railway 대시보드에서 **Variables** 탭 클릭 후 다음 추가:

| 변수명 | 값 | 설명 |
|--------|-----|------|
| `WALLET_ADDRESS` | `0x...` | BSC 지갑 주소 |
| `WALLET_PRIVATE_KEY` | `...` | 지갑 개인키 (절대 공유 금지!) |
| `TELEGRAM_BOT_TOKEN` | `123456789:ABC...` | 텔레그램 봇 토큰 (선택) |
| `TELEGRAM_CHAT_ID` | `123456789` | 텔레그램 Chat ID (선택) |

### 3.3 배포 시작
1. **Deploy** 버튼 클릭
2. 빌드 완료 대기 (2-3분)
3. **Logs** 탭에서 실행 확인

---

## 4. 봇 사용법

### 텔레그램 명령어
봇이 실행되면 텔레그램에서 제어 가능:

- `/start` - 메인 메뉴
- **주문 시작** - 메이커 주문 시작
- **주문 정지** - 메이커 주문 중지
- **상태** - 현재 주문/포인트 확인
- **잔고** - 지갑 잔고 확인
- **설정** - 레버리지/거리 등 변경

### 기본 설정값
| 설정 | 기본값 | 설명 |
|------|--------|------|
| 레버리지 | 40x | 자본 효율성 |
| 주문 거리 | 7.5 bps | Band A 내 (100% 포인트) |
| 주문 크기 | $450 | 심볼당 노출 금액 |
| 연속체결 보호 | 3회/60초 | 5분 정지 |

---

## 5. 설정 커스터마이징

### config.yaml 수정
Fork한 저장소에서 `config.yaml` 파일 수정 가능:

```yaml
strategy:
  leverage: 40           # 레버리지 (10~50)
  order_size_usd: 450    # 주문 크기
  order_distances_bps:
    - 7.5                # 주문 거리 (bps)
```

수정 후 GitHub에 push하면 Railway가 자동 재배포합니다.

---

## 6. 비용

### Railway
- **Hobby Plan**: $5/월 (500시간 포함)
- **실제 비용**: 약 $7-10/월 (24/7 기준)

### StandX
- 거래 수수료: 메이커 0.02%
- 체결 시에만 수수료 발생

---

## 7. 주의사항

### 보안
- **개인키는 절대 공유하지 마세요!**
- Railway 환경변수에만 저장
- GitHub에 개인키 커밋 금지

### 리스크
- 체결 시 손실 발생 가능 (스프레드 + 수수료)
- 급변장에서 연속 체결 위험
- 봇은 자동으로 연속체결 보호 발동

### 모니터링
- 텔레그램으로 상태 주기적 확인
- Railway Logs 탭에서 오류 확인
- 포지션 발생 시 자동 청산됨

---

## 8. 문제 해결

### 봇이 시작되지 않음
1. Railway Logs 확인
2. 환경변수 설정 확인 (WALLET_ADDRESS, WALLET_PRIVATE_KEY)

### 텔레그램 응답 없음
1. Bot Token, Chat ID 확인
2. 봇에게 먼저 `/start` 메시지 전송

### 주문이 안 들어감
1. StandX 잔고 확인 ($50 이상)
2. 텔레그램에서 "주문 시작" 버튼 클릭

### 연속체결 정지됨
1. 텔레그램에서 "정지해제" 버튼 클릭
2. 시장 상황 확인 후 재시작

---

## 9. 업데이트

원본 저장소가 업데이트되면:

1. Fork한 저장소에서 **Sync fork** 클릭
2. Railway가 자동 재배포

---

## 연락처

문제 발생 시 GitHub Issues에 등록하거나 원본 저장소 관리자에게 문의.
