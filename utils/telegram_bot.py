"""
텔레그램 봇 모듈
- 봇 상태 모니터링
- 원격 제어 (시작/중지)
- 설정 변경 (주문 크기 등)
- 잔고 기반 주문 가능 금액 계산
- 오류 알림
"""
import asyncio
import traceback
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Optional, Dict, Any
import requests

try:
    from utils.logger import get_logger
except ImportError:
    from standx_maker_bot.utils.logger import get_logger

logger = get_logger('telegram')


@dataclass
class TelegramConfig:
    """텔레그램 설정"""
    bot_token: str
    chat_id: str
    enabled: bool = True


class TelegramBot:
    """
    텔레그램 봇 - 모니터링 및 원격 제어

    기능:
    - /status: 현재 봇 상태 조회
    - /stop: 봇 중지
    - /start: 봇 시작 (중지 상태에서)
    - /stats: 통계 조회
    - /balance: 잔고 및 주문 가능 금액 (20x 레버리지)
    - /setsize <금액>: 주문 크기 변경
    - /config: 현재 설정 조회
    - 주기적 상태 리포트
    - 오류 발생 시 알림
    """

    def __init__(self, config: TelegramConfig):
        self.config = config
        self.base_url = f"https://api.telegram.org/bot{config.bot_token}"
        self._last_update_id = 0
        self._running = False
        self._poll_task: Optional[asyncio.Task] = None

        # 콜백 함수들
        self._on_stop: Optional[Callable] = None
        self._on_start: Optional[Callable] = None
        self._get_status: Optional[Callable] = None
        self._get_stats: Optional[Callable] = None
        self._get_balance: Optional[Callable] = None
        self._get_config: Optional[Callable] = None
        self._set_order_size: Optional[Callable] = None

    def set_callbacks(
        self,
        on_stop: Callable = None,
        on_start: Callable = None,
        get_status: Callable = None,
        get_stats: Callable = None,
        get_balance: Callable = None,
        get_config: Callable = None,
        set_order_size: Callable = None,
    ):
        """콜백 함수 설정"""
        self._on_stop = on_stop
        self._on_start = on_start
        self._get_status = get_status
        self._get_stats = get_stats
        self._get_balance = get_balance
        self._get_config = get_config
        self._set_order_size = set_order_size

    def send_message(self, text: str, parse_mode: str = "HTML") -> bool:
        """메시지 전송"""
        if not self.config.enabled:
            return False

        try:
            url = f"{self.base_url}/sendMessage"
            data = {
                "chat_id": self.config.chat_id,
                "text": text,
                "parse_mode": parse_mode,
            }
            response = requests.post(url, data=data, timeout=10)
            return response.status_code == 200
        except Exception as e:
            logger.error(f"텔레그램 메시지 전송 실패: {e}")
            return False

    def send_startup_message(self):
        """시작 메시지 전송"""
        msg = (
            "🚀 <b>StandX Maker Bot 시작</b>\n\n"
            "봇이 Railway에서 실행되었습니다.\n\n"
            "<b>사용 가능한 명령어:</b>\n"
            "/status - 현재 상태 조회\n"
            "/stats - 통계 조회\n"
            "/stop - 봇 중지\n"
            "/start - 봇 시작"
        )
        self.send_message(msg)

    def send_shutdown_message(self, reason: str = "정상 종료"):
        """종료 메시지 전송"""
        msg = f"🛑 <b>StandX Maker Bot 종료</b>\n\n사유: {reason}"
        self.send_message(msg)

    def send_error_message(self, error: str, traceback_str: str = None):
        """오류 메시지 전송"""
        msg = f"❌ <b>오류 발생</b>\n\n<code>{error}</code>"
        if traceback_str:
            # 트레이스백이 너무 길면 자르기
            if len(traceback_str) > 1000:
                traceback_str = traceback_str[:1000] + "..."
            msg += f"\n\n<pre>{traceback_str}</pre>"
        self.send_message(msg)

    def send_status_report(self, status: Dict[str, Any]):
        """상태 리포트 전송"""
        try:
            stats = status.get('stats', {})
            runtime = status.get('runtime_hours', 0)

            msg = (
                f"📊 <b>상태 리포트</b>\n\n"
                f"⏱ 실행 시간: {runtime:.2f}시간\n"
                f"📝 주문 생성: {stats.get('orders_placed', 0)}건\n"
                f"❌ 주문 취소: {stats.get('orders_cancelled', 0)}건\n"
                f"🔄 재배치: {stats.get('rebalances', 0)}회\n"
                f"⚠️ 체결: {stats.get('fills', 0)}건\n"
                f"💰 예상 포인트: {stats.get('estimated_points', 0):.1f}\n"
            )

            # 심볼별 상태
            symbols = status.get('symbols', {})
            for symbol, sym_status in symbols.items():
                mid_price = sym_status.get('mid_price', 0)
                spread = sym_status.get('spread_bps', 0)
                msg += f"\n<b>[{symbol}]</b>\n"
                msg += f"  Mid: ${mid_price:,.2f} | Spread: {spread:.1f}bps\n"

                if sym_status.get('buy_order'):
                    buy = sym_status['buy_order']
                    msg += f"  🟢 BUY: ${buy['price']:,.2f}\n"
                if sym_status.get('sell_order'):
                    sell = sym_status['sell_order']
                    msg += f"  🔴 SELL: ${sell['price']:,.2f}\n"

            self.send_message(msg)
        except Exception as e:
            logger.error(f"상태 리포트 전송 실패: {e}")

    async def _poll_updates(self):
        """텔레그램 업데이트 폴링"""
        while self._running:
            try:
                url = f"{self.base_url}/getUpdates"
                params = {
                    "offset": self._last_update_id + 1,
                    "timeout": 30,
                }

                response = requests.get(url, params=params, timeout=35)
                if response.status_code != 200:
                    await asyncio.sleep(5)
                    continue

                data = response.json()
                if not data.get('ok'):
                    await asyncio.sleep(5)
                    continue

                for update in data.get('result', []):
                    self._last_update_id = update['update_id']
                    await self._handle_update(update)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"텔레그램 폴링 오류: {e}")
                await asyncio.sleep(5)

    async def _handle_update(self, update: dict):
        """업데이트 처리"""
        message = update.get('message', {})
        text = message.get('text', '')
        chat_id = str(message.get('chat', {}).get('id', ''))

        # 허용된 chat_id만 처리
        if chat_id != self.config.chat_id:
            logger.warning(f"허용되지 않은 chat_id: {chat_id}")
            return

        # 명령어 처리
        if text.startswith('/'):
            parts = text.split()
            command = parts[0].lower()
            args = parts[1:] if len(parts) > 1 else []
            await self._handle_command(command, args)

    async def _handle_command(self, command: str, args: list = None):
        """명령어 처리"""
        args = args or []

        if command == '/status':
            if self._get_status:
                try:
                    status = self._get_status()
                    self.send_status_report(status)
                except Exception as e:
                    self.send_message(f"❌ 상태 조회 실패: {e}")
            else:
                self.send_message("❌ 상태 조회 기능이 설정되지 않았습니다.")

        elif command == '/stats':
            if self._get_stats:
                try:
                    stats = self._get_stats()
                    msg = (
                        f"📈 <b>통계</b>\n\n"
                        f"주문 생성: {stats.get('orders_placed', 0)}건\n"
                        f"주문 취소: {stats.get('orders_cancelled', 0)}건\n"
                        f"재배치: {stats.get('rebalances', 0)}회\n"
                        f"체결: {stats.get('fills', 0)}건\n"
                        f"예상 포인트: {stats.get('estimated_points', 0):.1f}"
                    )
                    self.send_message(msg)
                except Exception as e:
                    self.send_message(f"❌ 통계 조회 실패: {e}")
            else:
                self.send_message("❌ 통계 조회 기능이 설정되지 않았습니다.")

        elif command == '/balance':
            if self._get_balance:
                try:
                    balance_info = self._get_balance()
                    available = balance_info.get('available', 0)
                    equity = balance_info.get('equity', 0)
                    leverage = balance_info.get('leverage', 20)
                    margin_reserve = balance_info.get('margin_reserve_percent', 2)
                    current_order_size = balance_info.get('current_order_size', 0)

                    # 20x 레버리지로 주문 가능 금액 계산
                    usable_balance = available * (1 - margin_reserve / 100)
                    max_exposure = usable_balance * leverage

                    # 2+2 전략 (4개 주문) 기준 주문당 크기
                    recommended_per_order = max_exposure / 4

                    msg = (
                        f"💰 <b>잔고 및 주문 계산</b>\n\n"
                        f"<b>[ 계좌 잔고 ]</b>\n"
                        f"• 사용 가능: <code>${available:,.2f}</code>\n"
                        f"• 총 자산: <code>${equity:,.2f}</code>\n\n"
                        f"<b>[ {leverage}x 레버리지 계산 ]</b>\n"
                        f"• 마진 예약: {margin_reserve}%\n"
                        f"• 사용 가능 마진: <code>${usable_balance:,.2f}</code>\n"
                        f"• 최대 노출 금액: <code>${max_exposure:,.2f}</code>\n\n"
                        f"<b>[ 추천 주문 크기 (2+2 전략) ]</b>\n"
                        f"• 주문당 크기: <code>${recommended_per_order:,.0f}</code>\n"
                        f"• 현재 설정: <code>${current_order_size:,.0f}</code>\n\n"
                        f"💡 <i>/setsize {recommended_per_order:.0f} 로 변경 가능</i>"
                    )
                    self.send_message(msg)
                except Exception as e:
                    self.send_message(f"❌ 잔고 조회 실패: {e}")
            else:
                self.send_message("❌ 잔고 조회 기능이 설정되지 않았습니다.")

        elif command == '/setsize':
            if not args:
                self.send_message(
                    "⚠️ <b>사용법</b>: /setsize <금액>\n\n"
                    "예시: /setsize 3000\n"
                    "(레버리지 적용 후 주문당 노출 금액)"
                )
                return

            if self._set_order_size:
                try:
                    new_size = float(args[0])
                    if new_size < 10:
                        self.send_message("❌ 주문 크기는 최소 $10 이상이어야 합니다.")
                        return
                    if new_size > 100000:
                        self.send_message("❌ 주문 크기가 너무 큽니다 (최대 $100,000).")
                        return

                    result = self._set_order_size(new_size)
                    if result.get('success'):
                        old_size = result.get('old_size', 0)
                        leverage = result.get('leverage', 20)
                        required_margin = new_size / leverage

                        msg = (
                            f"✅ <b>주문 크기 변경 완료</b>\n\n"
                            f"• 이전: <code>${old_size:,.0f}</code>\n"
                            f"• 변경: <code>${new_size:,.0f}</code>\n"
                            f"• 필요 마진: <code>${required_margin:,.2f}</code> ({leverage}x)\n\n"
                            f"⚠️ 다음 주문부터 적용됩니다."
                        )
                        self.send_message(msg)
                    else:
                        self.send_message(f"❌ 변경 실패: {result.get('error', '알 수 없는 오류')}")
                except ValueError:
                    self.send_message("❌ 잘못된 금액 형식입니다. 숫자만 입력하세요.")
                except Exception as e:
                    self.send_message(f"❌ 주문 크기 변경 실패: {e}")
            else:
                self.send_message("❌ 주문 크기 변경 기능이 설정되지 않았습니다.")

        elif command == '/config':
            if self._get_config:
                try:
                    config = self._get_config()
                    strategy = config.get('strategy', {})
                    safety = config.get('safety', {})

                    msg = (
                        f"⚙️ <b>현재 설정</b>\n\n"
                        f"<b>[ 전략 설정 ]</b>\n"
                        f"• 심볼: {', '.join(strategy.get('symbols', []))}\n"
                        f"• 레버리지: {strategy.get('leverage', 20)}x\n"
                        f"• 주문 크기: <code>${strategy.get('order_size_usd', 0):,.0f}</code>\n"
                        f"• 마진 예약: {strategy.get('margin_reserve_percent', 2)}%\n"
                        f"• 전략: {strategy.get('num_orders_per_side', 2)}+{strategy.get('num_orders_per_side', 2)}\n"
                        f"• 주문 거리: {strategy.get('order_distances_bps', [])} bps\n\n"
                        f"<b>[ 안전 설정 ]</b>\n"
                        f"• 최대 포지션: <code>${safety.get('max_position_usd', 0):,.0f}</code>\n\n"
                        f"💡 <i>/setsize <금액> 으로 주문 크기 변경</i>"
                    )
                    self.send_message(msg)
                except Exception as e:
                    self.send_message(f"❌ 설정 조회 실패: {e}")
            else:
                self.send_message("❌ 설정 조회 기능이 설정되지 않았습니다.")

        elif command == '/stop':
            if self._on_stop:
                self.send_message("🛑 봇 중지 요청 중...")
                try:
                    await self._on_stop()
                    self.send_message("✅ 봇이 중지되었습니다.")
                except Exception as e:
                    self.send_message(f"❌ 봇 중지 실패: {e}")
            else:
                self.send_message("❌ 중지 기능이 설정되지 않았습니다.")

        elif command == '/start':
            if self._on_start:
                self.send_message("🚀 봇 시작 요청 중...")
                try:
                    await self._on_start()
                    self.send_message("✅ 봇이 시작되었습니다.")
                except Exception as e:
                    self.send_message(f"❌ 봇 시작 실패: {e}")
            else:
                self.send_message("❌ 시작 기능이 설정되지 않았습니다.")

        elif command == '/help':
            msg = (
                "📖 <b>사용 가능한 명령어</b>\n\n"
                "<b>[ 모니터링 ]</b>\n"
                "/status - 현재 상태 조회\n"
                "/stats - 통계 조회\n"
                "/balance - 잔고 및 주문 가능 금액\n\n"
                "<b>[ 설정 ]</b>\n"
                "/config - 현재 설정 조회\n"
                "/setsize <금액> - 주문 크기 변경\n\n"
                "<b>[ 제어 ]</b>\n"
                "/stop - 봇 중지\n"
                "/start - 봇 시작\n"
                "/help - 도움말"
            )
            self.send_message(msg)

        else:
            self.send_message(f"❓ 알 수 없는 명령어: {command}\n/help 로 도움말을 확인하세요.")

    async def start(self):
        """텔레그램 봇 시작"""
        if not self.config.enabled:
            logger.info("텔레그램 봇 비활성화됨")
            return

        self._running = True
        self._poll_task = asyncio.create_task(self._poll_updates())
        logger.info("텔레그램 봇 시작")
        self.send_startup_message()

    async def stop(self):
        """텔레그램 봇 중지"""
        self._running = False
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
        logger.info("텔레그램 봇 중지")


class TelegramNotifier:
    """
    간단한 텔레그램 알림 전송기
    (명령어 처리 없이 알림만 전송)
    """

    def __init__(self, bot_token: str, chat_id: str):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.base_url = f"https://api.telegram.org/bot{bot_token}"

    def send(self, text: str, parse_mode: str = "HTML") -> bool:
        """메시지 전송"""
        try:
            url = f"{self.base_url}/sendMessage"
            data = {
                "chat_id": self.chat_id,
                "text": text,
                "parse_mode": parse_mode,
            }
            response = requests.post(url, data=data, timeout=10)
            return response.status_code == 200
        except Exception as e:
            logger.error(f"텔레그램 전송 실패: {e}")
            return False

    def send_error(self, error: Exception):
        """오류 전송"""
        tb = traceback.format_exc()
        if len(tb) > 1000:
            tb = tb[:1000] + "..."
        msg = f"❌ <b>오류 발생</b>\n\n<code>{str(error)}</code>\n\n<pre>{tb}</pre>"
        self.send(msg)
