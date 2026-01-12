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
        self._close_all_positions: Optional[Callable] = None
        self._get_positions: Optional[Callable] = None
        self._set_leverage: Optional[Callable] = None
        self._set_strategy: Optional[Callable] = None
        self._set_distances: Optional[Callable] = None
        self._set_protection: Optional[Callable] = None
        self._enable_orders: Optional[Callable] = None
        self._disable_orders: Optional[Callable] = None
        self._is_orders_enabled: Optional[Callable] = None

        # 상태 리포트 주기 (초), 0이면 비활성화
        self._report_interval: float = 300.0

    def set_callbacks(
        self,
        on_stop: Callable = None,
        on_start: Callable = None,
        get_status: Callable = None,
        get_stats: Callable = None,
        get_balance: Callable = None,
        get_config: Callable = None,
        set_order_size: Callable = None,
        close_all_positions: Callable = None,
        get_positions: Callable = None,
        set_leverage: Callable = None,
        set_strategy: Callable = None,
        set_distances: Callable = None,
        set_protection: Callable = None,
        enable_orders: Callable = None,
        disable_orders: Callable = None,
        is_orders_enabled: Callable = None,
    ):
        """콜백 함수 설정"""
        self._on_stop = on_stop
        self._on_start = on_start
        self._get_status = get_status
        self._get_stats = get_stats
        self._get_balance = get_balance
        self._get_config = get_config
        self._set_order_size = set_order_size
        self._close_all_positions = close_all_positions
        self._get_positions = get_positions
        self._set_leverage = set_leverage
        self._set_strategy = set_strategy
        self._set_distances = set_distances
        self._set_protection = set_protection
        self._enable_orders = enable_orders
        self._disable_orders = disable_orders
        self._is_orders_enabled = is_orders_enabled

    def get_report_interval(self) -> float:
        """현재 리포트 주기 반환"""
        return self._report_interval

    def set_report_interval(self, interval: float):
        """리포트 주기 변경"""
        self._report_interval = interval

    def send_message(self, text: str, parse_mode: str = "HTML", reply_markup: dict = None) -> bool:
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
            if reply_markup:
                import json
                data["reply_markup"] = json.dumps(reply_markup)
            response = requests.post(url, data=data, timeout=10)
            return response.status_code == 200
        except Exception as e:
            logger.error(f"텔레그램 메시지 전송 실패: {e}")
            return False

    def _get_main_menu_keyboard(self):
        """메인 메뉴 인라인 키보드"""
        # 주문 상태에 따라 버튼 텍스트 변경
        orders_enabled = False
        if self._is_orders_enabled:
            try:
                orders_enabled = self._is_orders_enabled()
            except:
                pass

        if orders_enabled:
            order_btn = {"text": "⏸️ 주문 정지", "callback_data": "orders_disable"}
        else:
            order_btn = {"text": "▶️ 주문 시작", "callback_data": "orders_enable"}

        return {
            "inline_keyboard": [
                [
                    order_btn,
                    {"text": "📊 상태", "callback_data": "status"},
                    {"text": "💰 잔고", "callback_data": "balance"},
                ],
                [
                    {"text": "📈 통계", "callback_data": "stats"},
                    {"text": "📋 포지션", "callback_data": "positions"},
                    {"text": "📐 주문크기", "callback_data": "setsize_menu"},
                ],
                [
                    {"text": "⚙️ 설정", "callback_data": "settings_menu"},
                    {"text": "❌ 포지션 청산", "callback_data": "closeall_confirm"},
                    {"text": "🛑 봇 종료", "callback_data": "stop"},
                ],
            ]
        }

    def _get_settings_menu_keyboard(self):
        """설정 메뉴 인라인 키보드"""
        return {
            "inline_keyboard": [
                [
                    {"text": "📊 레버리지", "callback_data": "settings_leverage"},
                    {"text": "🎯 전략", "callback_data": "settings_strategy"},
                ],
                [
                    {"text": "📏 주문거리", "callback_data": "settings_distance"},
                    {"text": "🛡️ 체결보호", "callback_data": "settings_protection"},
                ],
                [
                    {"text": "📱 리포트주기", "callback_data": "settings_report"},
                ],
                [{"text": "↩️ 메뉴로 돌아가기", "callback_data": "menu"}],
            ]
        }

    def _get_leverage_keyboard(self):
        """레버리지 선택 키보드"""
        return {
            "inline_keyboard": [
                [
                    {"text": "10x", "callback_data": "set_leverage_10"},
                    {"text": "15x", "callback_data": "set_leverage_15"},
                    {"text": "20x", "callback_data": "set_leverage_20"},
                ],
                [{"text": "↩️ 설정으로", "callback_data": "settings_menu"}],
            ]
        }

    def _get_strategy_keyboard(self):
        """전략 선택 키보드"""
        return {
            "inline_keyboard": [
                [
                    {"text": "1+1 (안전)", "callback_data": "set_strategy_1"},
                    {"text": "2+2 (표준)", "callback_data": "set_strategy_2"},
                ],
                [{"text": "↩️ 설정으로", "callback_data": "settings_menu"}],
            ]
        }

    def _get_distance_keyboard(self):
        """주문 거리 선택 키보드"""
        return {
            "inline_keyboard": [
                [
                    {"text": "보수적 (8-9bps)", "callback_data": "set_distance_conservative"},
                    {"text": "표준 (7-8.5bps)", "callback_data": "set_distance_standard"},
                ],
                [
                    {"text": "공격적 (6-7.5bps)", "callback_data": "set_distance_aggressive"},
                ],
                [{"text": "↩️ 설정으로", "callback_data": "settings_menu"}],
            ]
        }

    def _get_protection_keyboard(self):
        """체결 보호 설정 키보드"""
        return {
            "inline_keyboard": [
                [
                    {"text": "✅ 켜기", "callback_data": "set_protection_on"},
                    {"text": "❌ 끄기", "callback_data": "set_protection_off"},
                ],
                [{"text": "↩️ 설정으로", "callback_data": "settings_menu"}],
            ]
        }

    def _get_report_interval_keyboard(self):
        """리포트 주기 선택 키보드"""
        return {
            "inline_keyboard": [
                [
                    {"text": "1분", "callback_data": "set_report_60"},
                    {"text": "5분", "callback_data": "set_report_300"},
                    {"text": "10분", "callback_data": "set_report_600"},
                ],
                [
                    {"text": "30분", "callback_data": "set_report_1800"},
                    {"text": "끄기", "callback_data": "set_report_0"},
                ],
                [{"text": "↩️ 설정으로", "callback_data": "settings_menu"}],
            ]
        }

    def _get_closeall_confirm_keyboard(self):
        """포지션 청산 확인 키보드"""
        return {
            "inline_keyboard": [
                [
                    {"text": "⚠️ 예, 모두 청산", "callback_data": "closeall"},
                    {"text": "↩️ 취소", "callback_data": "menu"},
                ],
            ]
        }

    def _get_back_to_menu_keyboard(self):
        """메뉴로 돌아가기 키보드"""
        return {
            "inline_keyboard": [
                [{"text": "↩️ 메뉴로 돌아가기", "callback_data": "menu"}],
            ]
        }

    def _get_order_size_keyboard(self):
        """주문 크기 설정 키보드"""
        return {
            "inline_keyboard": [
                [
                    {"text": "30% 마진", "callback_data": "setsize_30"},
                    {"text": "50% 마진", "callback_data": "setsize_50"},
                ],
                [
                    {"text": "🔥 최대 마진", "callback_data": "setsize_max"},
                ],
                [{"text": "↩️ 메뉴로 돌아가기", "callback_data": "menu"}],
            ]
        }

    def send_main_menu(self, text: str = None):
        """메인 메뉴 전송"""
        if text is None:
            text = "🤖 <b>StandX Maker Bot</b>\n\n원하는 기능을 선택하세요:"
        self.send_message(text, reply_markup=self._get_main_menu_keyboard())

    def send_startup_message(self):
        """시작 메시지 전송"""
        msg = (
            "🚀 <b>StandX Maker Bot 시작</b>\n\n"
            "봇이 Railway에서 실행되었습니다.\n\n"
            "아래 버튼으로 봇을 제어하세요:"
        )
        self.send_message(msg, reply_markup=self._get_main_menu_keyboard())

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

    def send_status_report(self, status: Dict[str, Any], with_menu: bool = True):
        """상태 리포트 전송"""
        try:
            stats = status.get('stats', {})
            runtime = status.get('runtime_hours', 0)

            uptime_percent = stats.get('uptime_percent', 0)
            msg = (
                f"📊 <b>상태 리포트</b>\n\n"
                f"⏱ 실행 시간: {runtime:.2f}시간\n"
                f"📈 업타임: {uptime_percent:.1f}%\n"
                f"📝 주문 생성: {stats.get('orders_placed', 0)}건\n"
                f"❌ 주문 취소: {stats.get('orders_cancelled', 0)}건\n"
                f"🔄 재배치: {stats.get('rebalances', 0)}회\n"
                f"⚠️ 체결: {stats.get('fills', 0)}건\n"
                f"💰 예상 포인트: {stats.get('estimated_points', 0):.1f}\n"
            )

            # 연속 체결 보호 상태 표시
            if status.get('consecutive_fill_paused'):
                remaining = status.get('consecutive_fill_pause_remaining', 0)
                level = status.get('consecutive_fill_escalation_level', 1)
                if remaining >= 3600:
                    remaining_str = f"{remaining / 3600:.1f}시간"
                else:
                    remaining_str = f"{remaining / 60:.0f}분"
                msg += f"\n🛑 <b>연속체결 {level}단계 일시정지:</b> {remaining_str} 남음\n"

            # 연속 체결 정지 횟수 표시
            pause_count = stats.get('consecutive_fill_pauses', 0)
            if pause_count > 0:
                msg += f"⏸ 연속체결 정지: {pause_count}회\n"

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

            if with_menu:
                self.send_message(msg, reply_markup=self._get_back_to_menu_keyboard())
            else:
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

    def _answer_callback_query(self, callback_query_id: str, text: str = None):
        """콜백 쿼리 응답 (버튼 클릭 시 로딩 해제)"""
        try:
            url = f"{self.base_url}/answerCallbackQuery"
            data = {"callback_query_id": callback_query_id}
            if text:
                data["text"] = text
            requests.post(url, data=data, timeout=5)
        except Exception as e:
            logger.error(f"콜백 쿼리 응답 실패: {e}")

    async def _handle_update(self, update: dict):
        """업데이트 처리"""
        # 콜백 쿼리 처리 (버튼 클릭)
        callback_query = update.get('callback_query')
        if callback_query:
            callback_id = callback_query.get('id')
            callback_data = callback_query.get('data', '')
            chat_id = str(callback_query.get('message', {}).get('chat', {}).get('id', ''))

            # 허용된 chat_id만 처리
            if chat_id != self.config.chat_id:
                logger.warning(f"허용되지 않은 chat_id (callback): {chat_id}")
                return

            # 버튼 로딩 해제
            self._answer_callback_query(callback_id)

            # 콜백 데이터 처리
            await self._handle_callback(callback_data)
            return

        # 일반 메시지 처리
        message = update.get('message', {})
        text = message.get('text', '')
        chat_id = str(message.get('chat', {}).get('id', ''))

        # 허용된 chat_id만 처리
        if chat_id and chat_id != self.config.chat_id:
            logger.warning(f"허용되지 않은 chat_id: {chat_id}")
            return

        # 명령어 처리
        if text.startswith('/'):
            parts = text.split()
            command = parts[0].lower()
            args = parts[1:] if len(parts) > 1 else []
            await self._handle_command(command, args)

    async def _handle_callback(self, callback_data: str):
        """콜백 데이터 처리 (버튼 클릭)"""
        if callback_data == 'menu':
            self.send_main_menu()

        elif callback_data == 'status':
            await self._handle_command('/status')

        elif callback_data == 'stats':
            await self._handle_command('/stats')

        elif callback_data == 'balance':
            await self._handle_command('/balance')

        elif callback_data == 'positions':
            await self._handle_command('/positions')

        elif callback_data == 'config':
            await self._handle_command('/config')

        elif callback_data == 'stop':
            await self._handle_command('/stop')

        # ========== 주문 시작/정지 ==========
        elif callback_data == 'orders_enable':
            logger.info("[텔레그램] 주문 시작 버튼 클릭됨")
            if self._enable_orders:
                try:
                    logger.info("[텔레그램] enable_orders() 호출 시작")
                    self._enable_orders()
                    logger.info("[텔레그램] enable_orders() 호출 완료")
                    self.send_message(
                        "✅ <b>주문 시작됨</b>\n\n"
                        "주문이 활성화되었습니다.\n"
                        "잠시 후 주문이 배치됩니다.",
                        reply_markup=self._get_main_menu_keyboard()
                    )
                except Exception as e:
                    logger.error(f"[텔레그램] enable_orders() 실패: {e}")
                    self.send_message(f"❌ 주문 시작 실패: {e}", reply_markup=self._get_main_menu_keyboard())
            else:
                logger.warning("[텔레그램] enable_orders 콜백이 설정되지 않음")
                self.send_message("❌ 주문 시작 기능이 설정되지 않았습니다.", reply_markup=self._get_main_menu_keyboard())

        elif callback_data == 'orders_disable':
            if self._disable_orders:
                try:
                    self._disable_orders()
                    self.send_message(
                        "⏸️ <b>주문 정지됨</b>\n\n"
                        "주문이 비활성화되었습니다.\n"
                        "기존 주문이 취소됩니다.",
                        reply_markup=self._get_main_menu_keyboard()
                    )
                except Exception as e:
                    self.send_message(f"❌ 주문 정지 실패: {e}", reply_markup=self._get_main_menu_keyboard())
            else:
                self.send_message("❌ 주문 정지 기능이 설정되지 않았습니다.", reply_markup=self._get_main_menu_keyboard())

        elif callback_data == 'closeall_confirm':
            # 청산 확인 메시지
            if self._get_positions:
                try:
                    positions = self._get_positions()
                    if not positions:
                        self.send_message("📭 종료할 포지션이 없습니다.", reply_markup=self._get_back_to_menu_keyboard())
                        return

                    msg = "⚠️ <b>모든 포지션을 시장가로 청산하시겠습니까?</b>\n\n"
                    total_pnl = 0
                    for pos in positions:
                        side_emoji = "🟢" if pos['side'] == 'long' else "🔴"
                        pnl = pos['unrealized_pnl']
                        total_pnl += pnl
                        msg += f"{side_emoji} {pos['symbol']} {pos['side'].upper()} {pos['size']:.4f} (PnL: ${pnl:+,.2f})\n"

                    pnl_emoji = "📈" if total_pnl >= 0 else "📉"
                    msg += f"\n{pnl_emoji} <b>총 PnL: ${total_pnl:+,.2f}</b>"

                    self.send_message(msg, reply_markup=self._get_closeall_confirm_keyboard())
                except Exception as e:
                    self.send_message(f"❌ 포지션 조회 실패: {e}", reply_markup=self._get_back_to_menu_keyboard())
            else:
                self.send_message("❌ 포지션 조회 기능이 설정되지 않았습니다.", reply_markup=self._get_back_to_menu_keyboard())

        elif callback_data == 'closeall':
            await self._handle_command('/closeall')

        elif callback_data == 'setsize_menu':
            # 주문 크기 설정 메뉴 표시
            await self._show_setsize_menu()

        elif callback_data.startswith('setsize_'):
            # 주문 크기 변경 (30%, 50%, max)
            await self._handle_setsize_callback(callback_data)

        # ========== 설정 메뉴 ==========
        elif callback_data == 'settings_menu':
            await self._show_settings_menu()

        elif callback_data == 'settings_leverage':
            await self._show_leverage_menu()

        elif callback_data == 'settings_strategy':
            await self._show_strategy_menu()

        elif callback_data == 'settings_distance':
            await self._show_distance_menu()

        elif callback_data == 'settings_protection':
            await self._show_protection_menu()

        elif callback_data == 'settings_report':
            await self._show_report_menu()

        # ========== 설정 변경 처리 ==========
        elif callback_data.startswith('set_leverage_'):
            await self._handle_leverage_callback(callback_data)

        elif callback_data.startswith('set_strategy_'):
            await self._handle_strategy_callback(callback_data)

        elif callback_data.startswith('set_distance_'):
            await self._handle_distance_callback(callback_data)

        elif callback_data.startswith('set_protection_'):
            await self._handle_protection_callback(callback_data)

        elif callback_data.startswith('set_report_'):
            await self._handle_report_callback(callback_data)

    async def _show_setsize_menu(self):
        """주문 크기 설정 메뉴 표시"""
        if self._get_balance:
            try:
                balance_info = self._get_balance()
                available = balance_info.get('available', 0)
                leverage = balance_info.get('leverage', 20)
                margin_reserve = balance_info.get('margin_reserve_percent', 2)
                current_order_size = balance_info.get('current_order_size', 0)

                # 사용 가능 마진 계산
                usable_balance = available * (1 - margin_reserve / 100)
                max_exposure = usable_balance * leverage

                # 2+2 전략 기준 주문당 크기
                size_30 = (max_exposure * 0.30) / 4
                size_50 = (max_exposure * 0.50) / 4
                size_max = max_exposure / 4

                msg = (
                    f"📐 <b>주문 크기 설정</b>\n\n"
                    f"<b>[ 현재 상태 ]</b>\n"
                    f"• 사용 가능 마진: <code>${usable_balance:,.2f}</code>\n"
                    f"• 최대 노출 ({leverage}x): <code>${max_exposure:,.0f}</code>\n"
                    f"• 현재 주문 크기: <code>${current_order_size:,.0f}</code>\n\n"
                    f"<b>[ 버튼 클릭 시 적용 ]</b>\n"
                    f"• 30% 마진: <code>${size_30:,.0f}</code>/주문\n"
                    f"• 50% 마진: <code>${size_50:,.0f}</code>/주문\n"
                    f"• 최대 마진: <code>${size_max:,.0f}</code>/주문\n\n"
                    f"<i>2+2 전략 기준 (4개 주문)</i>"
                )
                self.send_message(msg, reply_markup=self._get_order_size_keyboard())
            except Exception as e:
                self.send_message(f"❌ 잔고 조회 실패: {e}", reply_markup=self._get_back_to_menu_keyboard())
        else:
            self.send_message("❌ 잔고 조회 기능이 설정되지 않았습니다.", reply_markup=self._get_back_to_menu_keyboard())

    async def _handle_setsize_callback(self, callback_data: str):
        """주문 크기 버튼 클릭 처리"""
        if not self._get_balance or not self._set_order_size:
            self.send_message("❌ 기능이 설정되지 않았습니다.", reply_markup=self._get_back_to_menu_keyboard())
            return

        try:
            balance_info = self._get_balance()
            available = balance_info.get('available', 0)
            leverage = balance_info.get('leverage', 20)
            margin_reserve = balance_info.get('margin_reserve_percent', 2)

            # 사용 가능 마진 계산
            usable_balance = available * (1 - margin_reserve / 100)
            max_exposure = usable_balance * leverage

            # 비율에 따른 주문 크기 계산
            if callback_data == 'setsize_30':
                new_size = (max_exposure * 0.30) / 4
                percent_str = "30%"
            elif callback_data == 'setsize_50':
                new_size = (max_exposure * 0.50) / 4
                percent_str = "50%"
            elif callback_data == 'setsize_max':
                new_size = max_exposure / 4
                percent_str = "최대"
            else:
                return

            # 최소값 검사
            if new_size < 10:
                self.send_message(
                    f"❌ 계산된 주문 크기 (${new_size:.0f})가 너무 작습니다.\n"
                    f"최소 $10 이상이어야 합니다.",
                    reply_markup=self._get_back_to_menu_keyboard()
                )
                return

            # 주문 크기 변경 (즉시 재배치 포함)
            result = self._set_order_size(new_size, force_rebalance=True)
            if result and result.get('success'):
                old_size = result.get('old_size', 0)
                required_margin = new_size / leverage
                rebalanced = result.get('rebalanced', False)

                msg = (
                    f"✅ <b>주문 크기 변경 완료</b>\n\n"
                    f"• 설정: <b>{percent_str} 마진</b>\n"
                    f"• 이전: <code>${old_size:,.0f}</code>\n"
                    f"• 변경: <code>${new_size:,.0f}</code>\n"
                    f"• 필요 마진: <code>${required_margin:,.2f}</code> ({leverage}x)\n\n"
                )
                if rebalanced:
                    msg += "🔄 <b>기존 주문 취소 후 새 크기로 재배치 중...</b>"
                else:
                    msg += "⚠️ 다음 주문부터 적용됩니다."
                self.send_message(msg, reply_markup=self._get_back_to_menu_keyboard())
            else:
                error = result.get('error', '알 수 없는 오류') if result else '알 수 없는 오류'
                self.send_message(f"❌ 변경 실패: {error}", reply_markup=self._get_back_to_menu_keyboard())

        except Exception as e:
            self.send_message(f"❌ 주문 크기 변경 실패: {e}", reply_markup=self._get_back_to_menu_keyboard())

    # ========== 설정 메뉴 표시 함수들 ==========

    async def _show_settings_menu(self):
        """설정 메뉴 표시"""
        if self._get_config:
            try:
                config = self._get_config()
                strategy = config.get('strategy', {})

                msg = (
                    f"⚙️ <b>설정 메뉴</b>\n\n"
                    f"<b>[ 현재 설정 ]</b>\n"
                    f"• 레버리지: <code>{strategy.get('leverage', 20)}x</code>\n"
                    f"• 전략: <code>{strategy.get('num_orders_per_side', 2)}+{strategy.get('num_orders_per_side', 2)}</code>\n"
                    f"• 주문 거리: <code>{strategy.get('order_distances_bps', [])} bps</code>\n"
                    f"• 리포트 주기: <code>{self._report_interval / 60:.0f}분</code>\n\n"
                    f"변경할 설정을 선택하세요:"
                )
                self.send_message(msg, reply_markup=self._get_settings_menu_keyboard())
            except Exception as e:
                self.send_message(f"❌ 설정 조회 실패: {e}", reply_markup=self._get_back_to_menu_keyboard())
        else:
            self.send_message("⚙️ <b>설정 메뉴</b>\n\n변경할 설정을 선택하세요:",
                            reply_markup=self._get_settings_menu_keyboard())

    async def _show_leverage_menu(self):
        """레버리지 설정 메뉴 표시"""
        current = 20
        if self._get_config:
            try:
                config = self._get_config()
                current = config.get('strategy', {}).get('leverage', 20)
            except:
                pass

        msg = (
            f"📊 <b>레버리지 설정</b>\n\n"
            f"현재: <code>{current}x</code>\n\n"
            f"⚠️ 레버리지를 높이면 수익/손실이 증가합니다.\n"
            f"동일 마진으로 더 큰 포지션을 잡을 수 있습니다."
        )
        self.send_message(msg, reply_markup=self._get_leverage_keyboard())

    async def _show_strategy_menu(self):
        """전략 설정 메뉴 표시"""
        current = 2
        if self._get_config:
            try:
                config = self._get_config()
                current = config.get('strategy', {}).get('num_orders_per_side', 2)
            except:
                pass

        msg = (
            f"🎯 <b>전략 설정</b>\n\n"
            f"현재: <code>{current}+{current}</code>\n\n"
            f"<b>1+1</b>: 매수/매도 각 1개 주문\n"
            f"• 관리 간단, 체결 위험 낮음\n\n"
            f"<b>2+2</b>: 매수/매도 각 2개 주문\n"
            f"• 포인트 적립 효율 높음\n"
            f"• 더 넓은 가격대 커버"
        )
        self.send_message(msg, reply_markup=self._get_strategy_keyboard())

    async def _show_distance_menu(self):
        """주문 거리 설정 메뉴 표시"""
        current = [7.5, 8.5]
        if self._get_config:
            try:
                config = self._get_config()
                current = config.get('strategy', {}).get('order_distances_bps', [7.5, 8.5])
            except:
                pass

        msg = (
            f"📏 <b>주문 거리 설정</b>\n\n"
            f"현재: <code>{current} bps</code>\n\n"
            f"<b>보수적 (8-9bps)</b>\n"
            f"• Band A 경계에서 멀리 → 체결 위험 최소화\n\n"
            f"<b>표준 (7-8.5bps)</b>\n"
            f"• 균형잡힌 설정 (권장)\n\n"
            f"<b>공격적 (6-7.5bps)</b>\n"
            f"• 체결 위험 있으나 포인트 효율 극대화"
        )
        self.send_message(msg, reply_markup=self._get_distance_keyboard())

    async def _show_protection_menu(self):
        """체결 보호 설정 메뉴 표시"""
        msg = (
            f"🛡️ <b>연속 체결 보호</b>\n\n"
            f"연속 체결 시 자동으로 봇을 일시 정지합니다.\n\n"
            f"<b>켜기</b>: 1분 내 3회 체결 시 5분 정지\n"
            f"• 반복 체결 시 1시간까지 연장\n\n"
            f"<b>끄기</b>: 체결 상관없이 계속 운영\n"
            f"• 급변장에서 손실 위험 증가"
        )
        self.send_message(msg, reply_markup=self._get_protection_keyboard())

    async def _show_report_menu(self):
        """리포트 주기 설정 메뉴 표시"""
        current = self._report_interval
        if current == 0:
            current_str = "끄기"
        elif current < 60:
            current_str = f"{current}초"
        else:
            current_str = f"{current / 60:.0f}분"

        msg = (
            f"📱 <b>상태 리포트 주기</b>\n\n"
            f"현재: <code>{current_str}</code>\n\n"
            f"텔레그램으로 자동 상태 리포트를 받을 주기를 설정합니다.\n"
            f"'끄기'를 선택하면 수동 조회만 가능합니다."
        )
        self.send_message(msg, reply_markup=self._get_report_interval_keyboard())

    # ========== 설정 변경 핸들러 ==========

    async def _handle_leverage_callback(self, callback_data: str):
        """레버리지 변경 처리"""
        if not self._set_leverage:
            self.send_message("❌ 레버리지 변경 기능이 설정되지 않았습니다.",
                            reply_markup=self._get_settings_menu_keyboard())
            return

        try:
            leverage = int(callback_data.replace('set_leverage_', ''))
            result = self._set_leverage(leverage)

            if result and result.get('success'):
                old = result.get('old_leverage', 0)
                new = result.get('new_leverage', leverage)
                msg = (
                    f"✅ <b>레버리지 변경 완료</b>\n\n"
                    f"• 이전: <code>{old}x</code>\n"
                    f"• 변경: <code>{new}x</code>\n\n"
                    f"💡 주문 크기를 재설정하면 새 레버리지가 반영됩니다."
                )
                self.send_message(msg, reply_markup=self._get_settings_menu_keyboard())
            else:
                error = result.get('error', '알 수 없는 오류') if result else '알 수 없는 오류'
                self.send_message(f"❌ 변경 실패: {error}", reply_markup=self._get_settings_menu_keyboard())
        except Exception as e:
            self.send_message(f"❌ 레버리지 변경 실패: {e}", reply_markup=self._get_settings_menu_keyboard())

    async def _handle_strategy_callback(self, callback_data: str):
        """전략 변경 처리"""
        if not self._set_strategy:
            self.send_message("❌ 전략 변경 기능이 설정되지 않았습니다.",
                            reply_markup=self._get_settings_menu_keyboard())
            return

        try:
            num_orders = int(callback_data.replace('set_strategy_', ''))
            result = self._set_strategy(num_orders)

            if result and result.get('success'):
                old = result.get('old_strategy', '')
                new = result.get('new_strategy', f'{num_orders}+{num_orders}')
                msg = (
                    f"✅ <b>전략 변경 완료</b>\n\n"
                    f"• 이전: <code>{old}</code>\n"
                    f"• 변경: <code>{new}</code>\n\n"
                    f"🔄 기존 주문 취소 후 재배치 중..."
                )
                self.send_message(msg, reply_markup=self._get_settings_menu_keyboard())
            else:
                error = result.get('error', '알 수 없는 오류') if result else '알 수 없는 오류'
                self.send_message(f"❌ 변경 실패: {error}", reply_markup=self._get_settings_menu_keyboard())
        except Exception as e:
            self.send_message(f"❌ 전략 변경 실패: {e}", reply_markup=self._get_settings_menu_keyboard())

    async def _handle_distance_callback(self, callback_data: str):
        """주문 거리 변경 처리"""
        if not self._set_distances:
            self.send_message("❌ 주문 거리 변경 기능이 설정되지 않았습니다.",
                            reply_markup=self._get_settings_menu_keyboard())
            return

        try:
            preset = callback_data.replace('set_distance_', '')
            preset_names = {
                'conservative': '보수적 (8-9bps)',
                'standard': '표준 (7-8.5bps)',
                'aggressive': '공격적 (6-7.5bps)',
            }
            result = self._set_distances(preset)

            if result and result.get('success'):
                old = result.get('old_distances', [])
                new = result.get('new_distances', [])
                preset_name = preset_names.get(preset, preset)
                msg = (
                    f"✅ <b>주문 거리 변경 완료</b>\n\n"
                    f"• 설정: <b>{preset_name}</b>\n"
                    f"• 이전: <code>{old} bps</code>\n"
                    f"• 변경: <code>{new} bps</code>\n\n"
                    f"🔄 기존 주문 취소 후 재배치 중..."
                )
                self.send_message(msg, reply_markup=self._get_settings_menu_keyboard())
            else:
                error = result.get('error', '알 수 없는 오류') if result else '알 수 없는 오류'
                self.send_message(f"❌ 변경 실패: {error}", reply_markup=self._get_settings_menu_keyboard())
        except Exception as e:
            self.send_message(f"❌ 주문 거리 변경 실패: {e}", reply_markup=self._get_settings_menu_keyboard())

    async def _handle_protection_callback(self, callback_data: str):
        """체결 보호 설정 처리"""
        if not self._set_protection:
            self.send_message("❌ 체결 보호 설정 기능이 설정되지 않았습니다.",
                            reply_markup=self._get_settings_menu_keyboard())
            return

        try:
            enabled = callback_data == 'set_protection_on'
            result = self._set_protection(enabled)

            if result and result.get('success'):
                status = "켜짐 ✅" if enabled else "꺼짐 ❌"
                msg = (
                    f"✅ <b>연속 체결 보호 변경 완료</b>\n\n"
                    f"• 상태: <b>{status}</b>\n"
                )
                if not enabled:
                    msg += "\n⚠️ 급변장에서 연속 체결 시 손실 위험이 있습니다."
                self.send_message(msg, reply_markup=self._get_settings_menu_keyboard())
            else:
                error = result.get('error', '알 수 없는 오류') if result else '알 수 없는 오류'
                self.send_message(f"❌ 변경 실패: {error}", reply_markup=self._get_settings_menu_keyboard())
        except Exception as e:
            self.send_message(f"❌ 체결 보호 설정 실패: {e}", reply_markup=self._get_settings_menu_keyboard())

    async def _handle_report_callback(self, callback_data: str):
        """리포트 주기 변경 처리"""
        try:
            interval = int(callback_data.replace('set_report_', ''))
            self._report_interval = float(interval)

            if interval == 0:
                interval_str = "끄기"
            elif interval < 60:
                interval_str = f"{interval}초"
            else:
                interval_str = f"{interval / 60:.0f}분"

            msg = (
                f"✅ <b>리포트 주기 변경 완료</b>\n\n"
                f"• 주기: <b>{interval_str}</b>\n"
            )
            if interval == 0:
                msg += "\n💡 /status 명령으로 수동 조회하세요."
            self.send_message(msg, reply_markup=self._get_settings_menu_keyboard())
        except Exception as e:
            self.send_message(f"❌ 리포트 주기 변경 실패: {e}", reply_markup=self._get_settings_menu_keyboard())

    async def _handle_command(self, command: str, args: list = None):
        """명령어 처리"""
        args = args or []

        if command == '/status':
            if self._get_status:
                try:
                    status = self._get_status()
                    self.send_status_report(status)
                except Exception as e:
                    self.send_message(f"❌ 상태 조회 실패: {e}", reply_markup=self._get_back_to_menu_keyboard())
            else:
                self.send_message("❌ 상태 조회 기능이 설정되지 않았습니다.", reply_markup=self._get_back_to_menu_keyboard())

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
                    self.send_message(msg, reply_markup=self._get_back_to_menu_keyboard())
                except Exception as e:
                    self.send_message(f"❌ 통계 조회 실패: {e}", reply_markup=self._get_back_to_menu_keyboard())
            else:
                self.send_message("❌ 통계 조회 기능이 설정되지 않았습니다.", reply_markup=self._get_back_to_menu_keyboard())

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
                    self.send_message(msg, reply_markup=self._get_back_to_menu_keyboard())
                except Exception as e:
                    self.send_message(f"❌ 잔고 조회 실패: {e}", reply_markup=self._get_back_to_menu_keyboard())
            else:
                self.send_message("❌ 잔고 조회 기능이 설정되지 않았습니다.", reply_markup=self._get_back_to_menu_keyboard())

        elif command == '/setsize':
            if not args:
                self.send_message(
                    "⚠️ <b>사용법</b>: /setsize <금액>\n\n"
                    "예시: /setsize 3000\n"
                    "(레버리지 적용 후 주문당 노출 금액)",
                    reply_markup=self._get_back_to_menu_keyboard()
                )
                return

            if self._set_order_size:
                try:
                    new_size = float(args[0])
                    if new_size < 10:
                        self.send_message("❌ 주문 크기는 최소 $10 이상이어야 합니다.", reply_markup=self._get_back_to_menu_keyboard())
                        return
                    if new_size > 100000:
                        self.send_message("❌ 주문 크기가 너무 큽니다 (최대 $100,000).", reply_markup=self._get_back_to_menu_keyboard())
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
                        self.send_message(msg, reply_markup=self._get_back_to_menu_keyboard())
                    else:
                        self.send_message(f"❌ 변경 실패: {result.get('error', '알 수 없는 오류')}", reply_markup=self._get_back_to_menu_keyboard())
                except ValueError:
                    self.send_message("❌ 잘못된 금액 형식입니다. 숫자만 입력하세요.", reply_markup=self._get_back_to_menu_keyboard())
                except Exception as e:
                    self.send_message(f"❌ 주문 크기 변경 실패: {e}", reply_markup=self._get_back_to_menu_keyboard())
            else:
                self.send_message("❌ 주문 크기 변경 기능이 설정되지 않았습니다.", reply_markup=self._get_back_to_menu_keyboard())

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
                    self.send_message(msg, reply_markup=self._get_back_to_menu_keyboard())
                except Exception as e:
                    self.send_message(f"❌ 설정 조회 실패: {e}", reply_markup=self._get_back_to_menu_keyboard())
            else:
                self.send_message("❌ 설정 조회 기능이 설정되지 않았습니다.", reply_markup=self._get_back_to_menu_keyboard())

        elif command == '/positions':
            if self._get_positions:
                try:
                    positions = self._get_positions()
                    if not positions:
                        self.send_message("📭 현재 열린 포지션이 없습니다.", reply_markup=self._get_back_to_menu_keyboard())
                        return

                    msg = "📊 <b>현재 포지션</b>\n\n"
                    total_pnl = 0
                    for pos in positions:
                        side_emoji = "🟢" if pos['side'] == 'long' else "🔴"
                        pnl = pos['unrealized_pnl']
                        total_pnl += pnl
                        pnl_emoji = "📈" if pnl >= 0 else "📉"

                        msg += (
                            f"{side_emoji} <b>{pos['symbol']}</b> {pos['side'].upper()}\n"
                            f"   크기: <code>{pos['size']:.4f}</code>\n"
                            f"   진입가: <code>${pos['entry_price']:,.2f}</code>\n"
                            f"   현재가: <code>${pos['mark_price']:,.2f}</code>\n"
                            f"   {pnl_emoji} PnL: <code>${pnl:+,.2f}</code>\n\n"
                        )

                    pnl_emoji = "📈" if total_pnl >= 0 else "📉"
                    msg += f"━━━━━━━━━━━━━━\n{pnl_emoji} <b>총 PnL: <code>${total_pnl:+,.2f}</code></b>"
                    self.send_message(msg, reply_markup=self._get_back_to_menu_keyboard())
                except Exception as e:
                    self.send_message(f"❌ 포지션 조회 실패: {e}", reply_markup=self._get_back_to_menu_keyboard())
            else:
                self.send_message("❌ 포지션 조회 기능이 설정되지 않았습니다.", reply_markup=self._get_back_to_menu_keyboard())

        elif command == '/closeall':
            if self._close_all_positions:
                # 먼저 현재 포지션 확인
                if self._get_positions:
                    try:
                        positions = self._get_positions()
                        if not positions:
                            self.send_message("📭 종료할 포지션이 없습니다.", reply_markup=self._get_back_to_menu_keyboard())
                            return

                        # 포지션 정보 표시
                        msg = "⚠️ <b>다음 포지션을 시장가로 종료합니다:</b>\n\n"
                        for pos in positions:
                            side_emoji = "🟢" if pos['side'] == 'long' else "🔴"
                            msg += f"{side_emoji} {pos['symbol']} {pos['side'].upper()} {pos['size']:.4f}\n"
                        msg += "\n⏳ 모든 주문 취소 후 포지션 종료 중..."
                        self.send_message(msg)
                    except Exception as e:
                        logger.error(f"포지션 확인 실패: {e}")

                # ★ 먼저 모든 주문 비활성화 (주문 취소됨)
                if self._disable_orders:
                    try:
                        self._disable_orders()
                        logger.info("[포지션청산] 주문 비활성화 완료")
                    except Exception as e:
                        logger.error(f"주문 비활성화 실패: {e}")

                # 포지션 종료 실행
                try:
                    result = self._close_all_positions()
                    if result.get('success'):
                        closed = result.get('closed', [])
                        if closed:
                            msg = "✅ <b>포지션 종료 완료</b>\n\n"
                            msg += "• 모든 주문 취소됨\n"
                            for c in closed:
                                msg += f"• {c['symbol']}: {c['side']} {c['size']:.4f} 종료\n"
                            self.send_message(msg, reply_markup=self._get_back_to_menu_keyboard())
                        else:
                            self.send_message("📭 종료할 포지션이 없었습니다.\n• 모든 주문 취소됨", reply_markup=self._get_back_to_menu_keyboard())
                    else:
                        error = result.get('error', '알 수 없는 오류')
                        self.send_message(f"❌ 포지션 종료 실패: {error}\n• 주문은 취소됨", reply_markup=self._get_back_to_menu_keyboard())
                except Exception as e:
                    self.send_message(f"❌ 포지션 종료 실패: {e}", reply_markup=self._get_back_to_menu_keyboard())
            else:
                self.send_message("❌ 포지션 종료 기능이 설정되지 않았습니다.", reply_markup=self._get_back_to_menu_keyboard())

        elif command == '/stop':
            if self._on_stop:
                self.send_message("🛑 모든 주문 취소 후 봇 중지 중...")

                # ★ 먼저 모든 주문 비활성화 (주문 취소됨)
                if self._disable_orders:
                    try:
                        self._disable_orders()
                        logger.info("[봇종료] 주문 비활성화 완료")
                    except Exception as e:
                        logger.error(f"주문 비활성화 실패: {e}")

                try:
                    await self._on_stop()
                    self.send_message("✅ 봇이 중지되었습니다.\n• 모든 주문 취소됨", reply_markup=self._get_back_to_menu_keyboard())
                except Exception as e:
                    self.send_message(f"❌ 봇 중지 실패: {e}", reply_markup=self._get_back_to_menu_keyboard())
            else:
                self.send_message("❌ 중지 기능이 설정되지 않았습니다.", reply_markup=self._get_back_to_menu_keyboard())

        elif command == '/start' or command == '/help' or command == '/menu':
            # /start, /help, /menu 모두 메인 메뉴 표시
            self.send_main_menu()

        else:
            self.send_message(f"❓ 알 수 없는 명령어: {command}\n/help 로 도움말을 확인하세요.")

    def _set_bot_commands(self):
        """봇 명령어 목록 등록 (/ 입력 시 힌트 표시)"""
        try:
            url = f"{self.base_url}/setMyCommands"
            commands = [
                {"command": "start", "description": "메인 메뉴 표시"},
                {"command": "menu", "description": "메인 메뉴 표시"},
                {"command": "status", "description": "현재 봇 상태 조회"},
                {"command": "stats", "description": "통계 조회"},
                {"command": "balance", "description": "잔고 및 주문 가능 금액"},
                {"command": "positions", "description": "현재 포지션 조회"},
                {"command": "config", "description": "현재 설정 조회"},
                {"command": "setsize", "description": "주문 크기 변경 (예: /setsize 3000)"},
                {"command": "closeall", "description": "모든 포지션 시장가 청산"},
                {"command": "stop", "description": "봇 중지"},
            ]
            import json
            response = requests.post(url, json={"commands": commands}, timeout=10)
            if response.status_code == 200:
                logger.info("텔레그램 봇 명령어 목록 등록 완료")
            else:
                logger.warning(f"텔레그램 명령어 등록 실패: {response.text}")
        except Exception as e:
            logger.error(f"텔레그램 명령어 등록 실패: {e}")

    async def start(self):
        """텔레그램 봇 시작"""
        if not self.config.enabled:
            logger.info("텔레그램 봇 비활성화됨")
            return

        # 봇 명령어 목록 등록
        self._set_bot_commands()

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
