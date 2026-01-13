"""
메이커 포인트 파밍 전략 (새 전략)
- Band A 경계 근처 (8-9 bps) 양방향 주문 유지
- Mark price 기준 밴드 계산
- Lock + Cooldown으로 Duration 극대화
- 동적 거리 계산 (spread/volatility)
"""
import asyncio
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

try:
    from api.rest_client import StandXRestClient, OrderSide
    from api.websocket_client import StandXWebSocket
    from api.binance_ws_client import BinanceWebSocket
    from core.price_tracker import PriceTracker
    from core.band_calculator import BandCalculator, Band
    from core.order_manager import OrderManager, ManagedOrder, ManagedOrderStatus
    from core.safety_guard import SafetyGuard, SafetyConfig, SafetyAction, SafetyEvent, PreKillConfig, HardKillConfig
    from core.fill_protection import (
        FillProtection,
        FillProtectionConfig as FillProtectionCoreConfig,
        BinanceProtectionConfig as BinanceProtectionCoreConfig,
        QueueProtectionConfig as QueueProtectionCoreConfig,
        ProtectionEvent,
    )
    from utils.config import Config, StrategyConfig
    from utils.logger import get_logger
except ImportError:
    from standx_maker_bot.api.rest_client import StandXRestClient, OrderSide
    from standx_maker_bot.api.websocket_client import StandXWebSocket
    from standx_maker_bot.api.binance_ws_client import BinanceWebSocket
    from standx_maker_bot.core.price_tracker import PriceTracker
    from standx_maker_bot.core.band_calculator import BandCalculator, Band
    from standx_maker_bot.core.order_manager import OrderManager, ManagedOrder, ManagedOrderStatus
    from standx_maker_bot.core.safety_guard import SafetyGuard, SafetyConfig, SafetyAction, SafetyEvent, PreKillConfig, HardKillConfig
    from standx_maker_bot.core.fill_protection import (
        FillProtection,
        FillProtectionConfig as FillProtectionCoreConfig,
        BinanceProtectionConfig as BinanceProtectionCoreConfig,
        QueueProtectionConfig as QueueProtectionCoreConfig,
        ProtectionEvent,
    )
    from standx_maker_bot.utils.config import Config, StrategyConfig
    from standx_maker_bot.utils.logger import get_logger

logger = get_logger('maker_farming')


@dataclass
class SymbolState:
    """심볼별 상태 (2+2 전략 지원)"""
    symbol: str
    # 2+2 전략: 리스트로 다중 주문 관리
    buy_orders: List[Optional[ManagedOrder]] = field(default_factory=list)
    sell_orders: List[Optional[ManagedOrder]] = field(default_factory=list)
    last_reference_price: float = 0  # mark price 기준
    last_rebalance_time: float = 0
    last_target_distances_bps: List[float] = field(default_factory=list)  # 각 주문의 목표 거리
    total_points_estimate: float = 0
    rebalance_cooldown_until: float = 0  # 쿨다운 종료 시간
    last_sync_time: float = 0  # 마지막 동기화 시간

    def get_active_buy_count(self) -> int:
        """활성 매수 주문 수"""
        return sum(1 for o in self.buy_orders if o and o.is_active)

    def get_active_sell_count(self) -> int:
        """활성 매도 주문 수"""
        return sum(1 for o in self.sell_orders if o and o.is_active)

    def get_total_notional(self) -> float:
        """총 노출 금액"""
        total = 0.0
        for o in self.buy_orders:
            if o and o.is_active:
                total += o.notional_usd
        for o in self.sell_orders:
            if o and o.is_active:
                total += o.notional_usd
        return total


@dataclass
class FarmingStats:
    """파밍 통계"""
    start_time: float = field(default_factory=time.time)
    total_orders_placed: int = 0
    total_orders_cancelled: int = 0
    total_rebalances: int = 0
    total_fills: int = 0  # 체결 수 (원하지 않는 것)
    total_liquidations: int = 0  # 자동 청산 수
    total_take_profits: int = 0  # 익절 수
    total_stop_losses: int = 0  # 손절 수
    total_timeouts: int = 0  # 타임아웃 청산 수
    estimated_points: float = 0  # 누적 포인트
    consecutive_fill_pauses: int = 0  # 연속 체결로 인한 일시 정지 횟수
    # 포인트 누적 계산용
    last_points_update: float = field(default_factory=time.time)  # 마지막 포인트 업데이트 시각
    total_uptime_seconds: float = 0  # 총 주문 유지 시간 (초)


@dataclass
class HeldPosition:
    """체결 후 홀딩 중인 포지션"""
    symbol: str
    side: OrderSide  # 포지션 방향 (BUY=롱, SELL=숏)
    quantity: float
    entry_price: float  # 진입가
    entry_time: float  # 진입 시각
    take_profit_pct: float = 1.0  # 익절 % (기본 1%)
    stop_loss_pct: float = 1.0  # 손절 % (기본 1%)
    timeout_seconds: float = 300.0  # 타임아웃 (기본 5분)


class MakerFarmingStrategy:
    """
    메이커 포인트 파밍 전략 (새 전략)

    핵심 로직:
    1. 각 심볼에 대해 매수/매도 limit order 배치
    2. **Mark price** 기준 Band A 경계 근처 (8-9 bps)에 주문
    3. 동적 거리 계산 (spread/volatility 기반)
    4. **Band 이탈 시에만** 재배치 (Duration 극대화)
    5. **Lock + Cooldown**으로 0.5초 유지 조건 충족
    6. Hard Kill 조건 시에만 Lock 무시하고 즉시 취소
    """

    def __init__(
        self,
        config: Config,
        rest_client: StandXRestClient,
        ws_client: StandXWebSocket,
    ):
        """
        Args:
            config: 전체 설정
            rest_client: REST 클라이언트
            ws_client: WebSocket 클라이언트
        """
        self.config = config
        self.rest_client = rest_client
        self.ws_client = ws_client

        # 핵심 컴포넌트
        self.price_tracker = PriceTracker(ws_client, rest_client)
        self.band_calculator = BandCalculator(
            band_warning_bps=config.strategy.band_warning_bps,
        )
        self.order_manager = OrderManager(rest_client, leverage=config.strategy.leverage)

        # Pre-Kill 설정 (신규 주문 일시 중단)
        pre_kill_config = PreKillConfig(
            volatility_threshold_bps=config.safety.pre_kill.volatility_threshold_bps,
            mark_mid_divergence_bps=config.safety.pre_kill.mark_mid_divergence_bps,
            pause_duration_seconds=config.safety.pre_kill.pause_duration_seconds,
        )

        # Hard Kill 설정
        hard_kill_config = HardKillConfig(
            min_spread_bps=config.safety.hard_kill.min_spread_bps,
            max_volatility_bps=config.safety.hard_kill.max_volatility_bps,
            stale_threshold_seconds=config.safety.hard_kill.stale_threshold_seconds,
        )

        self.safety_guard = SafetyGuard(
            self.price_tracker,
            self.order_manager,
            rest_client,
            SafetyConfig(
                cancel_if_within_bps=config.safety.cancel_if_within_bps,
                max_position_usd=config.safety.max_position_usd,
                pre_kill=pre_kill_config,
                hard_kill=hard_kill_config,
            ),
        )

        # Binance WebSocket (선행 감지용)
        self.binance_ws = BinanceWebSocket(use_1s_stream=True)

        # Fill Protection (체결 방지 보호 장치)
        fp_config = config.fill_protection
        fill_protection_config = FillProtectionCoreConfig(
            binance=BinanceProtectionCoreConfig(
                enabled=fp_config.binance.enabled,
                trigger_bps=fp_config.binance.trigger_bps,
                window_seconds=fp_config.binance.window_seconds,
                cooldown_seconds=fp_config.binance.cooldown_seconds,
            ),
            queue=QueueProtectionCoreConfig(
                enabled=fp_config.queue.enabled,
                drop_threshold_percent=fp_config.queue.drop_threshold_percent,
                window_seconds=fp_config.queue.window_seconds,
                min_queue_ahead_usd=fp_config.queue.min_queue_ahead_usd,
            ),
            check_interval_seconds=fp_config.check_interval_seconds,
        )

        self.fill_protection = FillProtection(
            binance_ws=self.binance_ws,
            standx_ws=ws_client,
            order_manager=self.order_manager,
            safety_guard=self.safety_guard,
            config=fill_protection_config,
        )

        # 상태
        self._symbol_states: Dict[str, SymbolState] = {}
        self._stats = FarmingStats()
        self._running = False
        self._pending_liquidations: List[Tuple[str, OrderSide, float]] = []  # 청산 대기열
        self._effective_order_size_usd: float = config.strategy.order_size_usd  # 마진 예약 적용된 주문 크기

        # 포지션 홀딩 상태 (체결 후 ±1% 익절/손절 대기)
        self._held_position: Optional[HeldPosition] = None  # 현재 홀딩 중인 포지션
        self._position_monitor_task: Optional[asyncio.Task] = None  # 포지션 모니터링 태스크

        # 연속 체결 보호 상태
        self._fill_timestamps: List[float] = []  # 체결 시각 리스트
        self._consecutive_fill_pause_until: float = 0  # 일시 정지 종료 시각
        self._consecutive_fill_escalation_level: int = 0  # 단계 (0=정상, 1=5분정지 후 재개, 2+=1시간정지)
        self._last_pause_end_time: float = 0  # 마지막 정지 종료 시각 (단계 리셋용)

        # 강제 재배치 요청 플래그
        self._force_rebalance_requested: bool = False

        # 주문 활성화 플래그 (텔레그램에서 시작/정지 제어)
        # 기본값: False (수동 시작) - 텔레그램에서 '주문 시작' 버튼 클릭 필요
        self._orders_enabled: bool = False

        # 모든 포지션 청산 요청 플래그 (연속 체결 보호 발동 시)
        self._request_close_all_positions: bool = False

        # 콜백 등록
        self.safety_guard.on_safety_event(self._on_safety_event)
        self.order_manager.on_order_update(self._on_order_update)
        self.fill_protection.on_protection_event(self._on_fill_protection_event)

    def _on_safety_event(self, event: SafetyEvent):
        """안전 이벤트 처리"""
        logger.warning(f"[Safety] {event.action.value}: {event.reason}")

        if event.action == SafetyAction.EMERGENCY_STOP:
            self._running = False

    def _on_fill_protection_event(self, event: ProtectionEvent):
        """Fill Protection 이벤트 처리"""
        logger.warning(
            f"[FillProtection] {event.action.value}: {event.symbol} - {event.reason} "
            f"(취소: {event.details.get('cancelled', 0)}건)"
        )

    def _check_consecutive_fills(self):
        """
        연속 체결 검사 및 자동 일시 정지 (단계적 강화)

        - 1단계: 1분 내 3회 체결 → 5분 정지
        - 2단계: 재개 후 또 3회 체결 → 1시간 정지
        - 30분간 체결 없으면 1단계로 리셋
        """
        cfp = self.config.consecutive_fill_protection
        if not cfp.enabled:
            return

        now = time.time()

        # 체결 시각 기록
        self._fill_timestamps.append(now)

        # 윈도우 밖 기록 제거
        window = cfp.window_seconds
        self._fill_timestamps = [t for t in self._fill_timestamps if now - t < window]

        # 연속 체결 횟수 확인
        fill_count = len(self._fill_timestamps)

        if fill_count >= cfp.max_fills:
            # 단계적 정지 시간 결정
            if self._consecutive_fill_escalation_level >= 1:
                # 2단계 이상: 1시간 정지
                pause_duration = cfp.escalated_pause_duration_seconds
                level_str = "2단계"
            else:
                # 1단계: 5분 정지
                pause_duration = cfp.pause_duration_seconds
                level_str = "1단계"

            # 자동 일시 정지 활성화
            self._consecutive_fill_pause_until = now + pause_duration
            self._consecutive_fill_escalation_level += 1
            self._stats.consecutive_fill_pauses += 1

            # 사람이 읽기 쉬운 시간 표시
            if pause_duration >= 3600:
                duration_str = f"{pause_duration / 3600:.1f}시간"
            else:
                duration_str = f"{pause_duration / 60:.0f}분"

            logger.critical(
                f"★★★ 연속 체결 감지! {fill_count}회/{cfp.window_seconds}초 → "
                f"{level_str} {duration_str} 일시 정지 ★★★"
            )

            # 체결 기록 초기화 (정지 후 재시작 시 새로 카운트)
            self._fill_timestamps.clear()

            # ★ 연속 체결 정지 시 모든 포지션 청산 요청
            self._request_close_all_positions = True
            logger.warning("★ 연속 체결 정지 - 모든 포지션 청산 예약됨")

    def is_consecutive_fill_paused(self) -> bool:
        """연속 체결로 인한 일시 정지 상태인지"""
        now = time.time()
        is_paused = now < self._consecutive_fill_pause_until

        # 정지 종료 시점 기록 (단계 리셋용)
        if not is_paused and self._consecutive_fill_pause_until > 0:
            if self._last_pause_end_time < self._consecutive_fill_pause_until:
                self._last_pause_end_time = now
                logger.info(f"[연속체결보호] 정지 종료 - 현재 단계: {self._consecutive_fill_escalation_level}")

        return is_paused

    def get_consecutive_fill_pause_remaining(self) -> float:
        """연속 체결 일시 정지 남은 시간 (초)"""
        return max(0, self._consecutive_fill_pause_until - time.time())

    def reset_consecutive_fill_pause(self) -> dict:
        """
        연속 체결 보호 정지 수동 해제 (텔레그램에서 호출)

        Returns:
            {'success': True, 'remaining_was': 남은시간, 'level_was': 단계}
        """
        remaining = self.get_consecutive_fill_pause_remaining()
        level = self._consecutive_fill_escalation_level

        # 정지 상태 초기화
        self._consecutive_fill_pause_until = 0
        self._consecutive_fill_escalation_level = 0
        self._last_pause_end_time = 0
        self._fill_timestamps.clear()

        logger.info(f"★ [연속체결보호] 수동 해제됨 (남은시간: {remaining:.0f}초, 단계: {level})")

        return {
            'success': True,
            'remaining_was': remaining,
            'level_was': level,
        }

    def request_force_rebalance(self):
        """
        강제 재배치 요청

        텔레그램에서 주문 크기/전략/거리 변경 시 호출됨.
        다음 루프에서 모든 기존 주문을 취소하고 새 설정으로 재배치함.
        """
        self._force_rebalance_requested = True
        # ★ 주문이 비활성화 상태면 활성화도 함께 (설정 변경 = 주문 시작 의도)
        if not self._orders_enabled:
            self._orders_enabled = True
            print("[강제재배치] 주문 비활성화 상태 → 활성화로 변경", flush=True)
            logger.info("[강제재배치] 주문 비활성화 상태 → 활성화로 변경")
        logger.info("[강제재배치] 요청됨 - 다음 루프에서 모든 주문 재배치")

        # ★ 즉시 주문 취소 (체결 방지)
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.create_task(self._cancel_all_orders_immediately())
                logger.info("[강제재배치] ★ 즉시 취소 태스크 시작됨")
        except Exception as e:
            logger.warning(f"[강제재배치] 즉시 취소 태스크 생성 실패: {e}")

    async def _cancel_all_orders_immediately(self):
        """설정 변경 시 모든 주문 즉시 취소 (체결 방지)"""
        try:
            logger.info("[즉시취소] ★★★ 모든 주문 취소 시작...")
            symbols = self.config.strategy.symbols
            for symbol in symbols:
                state = self._symbol_states.get(symbol)
                if state:
                    # Lock 해제 후 취소
                    for order in state.buy_orders + state.sell_orders:
                        if order and order.is_active:
                            self.safety_guard.clear_order_lock(order.cl_ord_id)
                    await self.order_manager.cancel_all_orders(symbol)
                    # ★ 상태도 초기화 (메인 루프에서 신규 배치하도록)
                    state.buy_orders = [None] * len(state.buy_orders)
                    state.sell_orders = [None] * len(state.sell_orders)
            logger.info("[즉시취소] ★★★ 모든 주문 취소 및 상태 초기화 완료")
        except Exception as e:
            logger.error(f"[즉시취소] 취소 실패: {e}")

    def enable_orders(self):
        """주문 활성화 (텔레그램에서 시작 버튼 클릭 시)"""
        print("[전략] ★★★ enable_orders() 호출됨", flush=True)
        self._orders_enabled = True
        self._force_rebalance_requested = True  # 즉시 주문 배치
        print(f"[전략] _orders_enabled={self._orders_enabled}, _force_rebalance_requested={self._force_rebalance_requested}", flush=True)
        logger.info("★ 주문 활성화됨 - 주문 배치 시작")

    def disable_orders(self):
        """주문 비활성화 및 기존 주문 취소 (텔레그램에서 정지 버튼 클릭 시)"""
        print("[전략] ★★★ disable_orders() 호출됨", flush=True)
        self._orders_enabled = False
        logger.info("★ 주문 비활성화됨 - 기존 주문 취소 시작")

        # ★ 즉시 모든 주문 취소 (비동기 태스크)
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.create_task(self._cancel_all_orders_immediately())
                print("[전략] 주문 취소 태스크 생성됨", flush=True)
                logger.info("[주문정지] ★ 즉시 취소 태스크 시작됨")
        except Exception as e:
            print(f"[전략] 주문 취소 태스크 생성 실패: {e}", flush=True)
            logger.warning(f"[주문정지] 즉시 취소 태스크 생성 실패: {e}")

    def is_orders_enabled(self) -> bool:
        """주문 활성화 상태 확인"""
        return self._orders_enabled

    def _check_escalation_reset(self):
        """단계 리셋 확인 (30분간 체결 없으면 1단계로)"""
        cfp = self.config.consecutive_fill_protection
        if not cfp.enabled:
            return

        now = time.time()

        # 정지 중이 아니고, 마지막 정지 종료 후 리셋 시간이 지났으면
        if (not self.is_consecutive_fill_paused() and
            self._consecutive_fill_escalation_level > 0 and
            self._last_pause_end_time > 0 and
            now - self._last_pause_end_time >= cfp.escalation_reset_seconds):

            logger.info(
                f"[연속체결보호] 단계 리셋: {self._consecutive_fill_escalation_level} → 0 "
                f"({cfp.escalation_reset_seconds / 60:.0f}분간 체결 없음)"
            )
            self._consecutive_fill_escalation_level = 0
            self._last_pause_end_time = 0

    def _on_order_update(self, order: ManagedOrder):
        """주문 업데이트 처리"""
        if order.status == ManagedOrderStatus.FILLED:
            # 청산 주문(mkt_)은 무시 - 무한 루프 방지
            if "_mkt_" in order.cl_ord_id:
                print(f"[체결] 청산 주문 체결 확인: {order.cl_ord_id}", flush=True)
                return

            self._stats.total_fills += 1
            print(f"[체결] ★★★ 주문 체결됨! {order.symbol} {order.side.value} {order.quantity} @ ${order.price:,.2f}", flush=True)
            logger.warning(f"★ 주문 체결: {order.symbol} {order.side.value} {order.quantity} @ ${order.price:,.2f}")

            # 연속 체결 보호: 체결 시각 기록 및 검사
            self._check_consecutive_fills()

            # 즉시 청산: 청산 대기열에 추가 (다음 루프에서 처리)
            # BUY 주문 체결 → SELL로 청산, SELL 주문 체결 → BUY로 청산
            close_side = OrderSide.SELL if order.side == OrderSide.BUY else OrderSide.BUY
            self._pending_liquidations.append((order.symbol, close_side, order.quantity))
            print(f"[체결] 즉시청산 대기열 추가: {order.symbol} {close_side.value} {order.quantity}", flush=True)

            # ★ 비동기 태스크로 즉시 청산 스케줄 (콜백이 동기라서 태스크 생성)
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    asyncio.create_task(self._execute_immediate_liquidation(order.symbol, close_side, order.quantity))
                    print(f"[체결] 즉시청산 태스크 생성됨", flush=True)
            except Exception as e:
                print(f"[체결] 즉시청산 태스크 생성 실패: {e}", flush=True)
                logger.error(f"[즉시청산] 태스크 생성 실패: {e} - 메인 루프에서 처리됨")

        elif order.status == ManagedOrderStatus.CANCELLED:
            self._stats.total_orders_cancelled += 1

    async def _execute_immediate_liquidation(self, symbol: str, side: OrderSide, quantity: float):
        """즉시 청산 실행 (비동기 태스크)"""
        try:
            print(f"[즉시청산태스크] ★★★ 실행 시작: {symbol} {side.value} {quantity}", flush=True)
            logger.warning(f"[즉시청산태스크] ★ 실행 시작: {symbol} {side.value} {quantity}")
            result = await self.order_manager.create_market_order(
                symbol=symbol,
                side=side,
                quantity=quantity,
                reduce_only=True,
            )
            if result:
                self._stats.total_liquidations += 1
                logger.warning(f"[즉시청산태스크] ✅ 성공: {symbol} {side.value} {quantity}")
                # 대기열에서 제거 (이미 처리됨)
                try:
                    self._pending_liquidations.remove((symbol, side, quantity))
                    logger.info(f"[즉시청산태스크] 대기열에서 제거됨")
                except ValueError:
                    logger.info(f"[즉시청산태스크] 대기열에 없음 (이미 처리됨)")
            else:
                logger.error(f"[즉시청산태스크] ❌ 실패 (result=None): {symbol} {side.value} {quantity}")
        except Exception as e:
            logger.error(f"[즉시청산태스크] ❌ 오류: {e}")

    async def _monitor_position_for_exit(self):
        """
        포지션 홀딩 모니터링 (±1% 익절/손절 + 타임아웃)

        체결 후 포지션을 홀딩하며 다음 조건에서 청산:
        - +1% 수익 → 익절
        - -1% 손실 → 손절
        - 5분 경과 → 타임아웃 시장가 청산
        """
        if not self._held_position:
            return

        pos = self._held_position
        logger.info(f"[포지션모니터링] 시작: {pos.symbol} {pos.side.value} @ ${pos.entry_price:,.2f}")

        # 먼저 모든 메이커 주문 취소 (포지션 홀딩 중에는 신규 주문 안 함)
        await self.order_manager.cancel_all_orders(pos.symbol)
        logger.info(f"[포지션홀딩] 메이커 주문 취소 완료 - 포지션 청산까지 대기")

        try:
            while self._running and self._held_position:
                now = time.time()
                elapsed = now - pos.entry_time

                # 현재 가격 조회
                price_info = self.price_tracker.get_price(pos.symbol)
                if not price_info:
                    await asyncio.sleep(0.5)
                    continue

                current_price = price_info.mark_price

                # 수익률 계산 (롱/숏에 따라 다름)
                if pos.side == OrderSide.BUY:
                    # 롱 포지션: 가격 상승 = 수익
                    pnl_pct = ((current_price - pos.entry_price) / pos.entry_price) * 100
                else:
                    # 숏 포지션: 가격 하락 = 수익
                    pnl_pct = ((pos.entry_price - current_price) / pos.entry_price) * 100

                # 청산 방향 (포지션 반대)
                close_side = OrderSide.SELL if pos.side == OrderSide.BUY else OrderSide.BUY

                # 익절 체크
                if pnl_pct >= pos.take_profit_pct:
                    logger.info(
                        f"★ [익절] {pos.symbol} +{pnl_pct:.2f}% "
                        f"(진입: ${pos.entry_price:,.2f} → 현재: ${current_price:,.2f})"
                    )
                    await self._close_held_position(close_side, "익절")
                    self._stats.total_take_profits += 1
                    break

                # 손절 체크
                if pnl_pct <= -pos.stop_loss_pct:
                    logger.warning(
                        f"★ [손절] {pos.symbol} {pnl_pct:.2f}% "
                        f"(진입: ${pos.entry_price:,.2f} → 현재: ${current_price:,.2f})"
                    )
                    await self._close_held_position(close_side, "손절")
                    self._stats.total_stop_losses += 1
                    break

                # 타임아웃 체크
                if elapsed >= pos.timeout_seconds:
                    logger.warning(
                        f"★ [타임아웃] {pos.symbol} {pnl_pct:.2f}% ({elapsed:.0f}초 경과) "
                        f"(진입: ${pos.entry_price:,.2f} → 현재: ${current_price:,.2f})"
                    )
                    await self._close_held_position(close_side, "타임아웃")
                    self._stats.total_timeouts += 1
                    break

                # 상태 로깅 (10초마다)
                if int(elapsed) % 10 == 0 and int(elapsed) > 0:
                    logger.debug(
                        f"[포지션홀딩] {pos.symbol} {pnl_pct:+.2f}% "
                        f"(경과: {elapsed:.0f}s/{pos.timeout_seconds:.0f}s)"
                    )

                await asyncio.sleep(0.5)

        except asyncio.CancelledError:
            logger.info("[포지션모니터링] 취소됨")
            # 태스크 취소 시에도 포지션이 있으면 청산
            if self._held_position:
                close_side = OrderSide.SELL if pos.side == OrderSide.BUY else OrderSide.BUY
                await self._close_held_position(close_side, "태스크취소")
        except Exception as e:
            logger.error(f"[포지션모니터링] 오류: {e}")
            # 오류 시에도 포지션 청산 시도
            if self._held_position:
                close_side = OrderSide.SELL if pos.side == OrderSide.BUY else OrderSide.BUY
                await self._close_held_position(close_side, "오류청산")

    async def _close_held_position(self, side: OrderSide, reason: str):
        """홀딩 포지션 청산"""
        if not self._held_position:
            return

        pos = self._held_position
        try:
            logger.info(f"[포지션청산] {reason}: {pos.symbol} {side.value} {pos.quantity}")
            result = await self.order_manager.create_market_order(
                symbol=pos.symbol,
                side=side,
                quantity=pos.quantity,
                reduce_only=True,
            )
            if result:
                logger.info(f"[포지션청산] 성공: {pos.symbol} {reason}")
            else:
                logger.error(f"[포지션청산] 실패: {pos.symbol}")
        except Exception as e:
            logger.error(f"[포지션청산] 오류: {e}")
        finally:
            # 포지션 홀딩 상태 초기화 → 메이커 주문 재개
            self._held_position = None
            logger.info("[포지션홀딩] 종료 - 메이커 주문 재개")

    async def _process_pending_liquidations(self):
        """대기 중인 청산 처리"""
        if self._pending_liquidations:
            logger.warning(f"[청산처리] 대기열 크기: {len(self._pending_liquidations)}")

        while self._pending_liquidations:
            symbol, side, quantity = self._pending_liquidations.pop(0)
            try:
                logger.warning(f"[청산처리] 실행 시작: {symbol} {side.value} {quantity}")
                result = await self.order_manager.create_market_order(
                    symbol=symbol,
                    side=side,
                    quantity=quantity,
                    reduce_only=True,
                )
                if result:
                    self._stats.total_liquidations += 1
                    logger.warning(f"[청산처리] ✅ 성공: {symbol} {side.value} {quantity}")
                else:
                    logger.error(f"[청산처리] ❌ 실패 (result=None): {symbol} {side.value} {quantity}")
            except Exception as e:
                logger.error(f"[청산처리] ❌ 오류: {e}")

    async def _position_check_loop(self):
        """
        포지션 확인 루프 (별도 태스크)

        메인 루프와 독립적으로 실행되어 포지션을 주기적으로 확인하고 청산.
        동기 API 호출이 메인 루프를 블로킹하지 않도록 분리함.
        """
        print("[포지션체크] ★ 포지션 확인 루프 시작", flush=True)
        while self._running:
            try:
                await self._check_and_liquidate_positions()
            except Exception as e:
                print(f"[포지션체크] 오류: {e}", flush=True)

            # 5초 간격으로 포지션 확인 (너무 자주 하면 API 부하)
            await asyncio.sleep(5)

        print("[포지션체크] 포지션 확인 루프 종료", flush=True)

    async def _check_and_liquidate_positions(self):
        """
        포지션 직접 확인 후 즉시 청산 (핵심 안전장치)

        주기적으로 실제 포지션을 API로 확인하고,
        포지션이 있으면 즉시 시장가로 청산

        단, 홀딩 모드 중에는 스킵 (±1% 익절/손절 대기 중)
        """
        # 포지션 홀딩 모드 중에는 자동 청산 스킵
        if self._held_position:
            return

        try:
            # ★ 동기 API 호출을 비동기로 실행 (이벤트 루프 블로킹 방지)
            positions = await asyncio.to_thread(self.rest_client.get_positions)

            for pos in positions:
                if pos.size > 0:
                    # 포지션 발견 - 즉시 청산
                    # SHORT면 BUY로, LONG이면 SELL로 청산
                    close_side = OrderSide.BUY if pos.side == 'short' else OrderSide.SELL

                    logger.error(
                        f"[{pos.symbol}] 포지션 발견! {pos.side.upper()} {pos.size} @ {pos.entry_price} "
                        f"(PnL: {pos.unrealized_pnl}) -> 즉시 청산"
                    )

                    try:
                        result = await self.order_manager.create_market_order(
                            symbol=pos.symbol,
                            side=close_side,
                            quantity=pos.size,
                            reduce_only=True,
                        )

                        if result:
                            self._stats.total_liquidations += 1
                            logger.info(f"[{pos.symbol}] 포지션 청산 성공!")
                        else:
                            logger.error(f"[{pos.symbol}] 포지션 청산 실패")

                    except Exception as e:
                        logger.error(f"[{pos.symbol}] 청산 오류: {e}")

        except Exception as e:
            logger.error(f"포지션 확인 오류: {e}")

    def _get_order_quantity(self, symbol: str, price: float, order_index: int = 0) -> float:
        """
        주문 수량 계산

        Args:
            symbol: 심볼
            price: 가격
            order_index: 주문 인덱스 (0=안쪽, 1=바깥쪽)

        Returns:
            수량
        """
        # USD 금액 / 가격 = 수량 (마진 예약 적용된 크기 사용)
        notional = self._effective_order_size_usd

        # 바깥쪽 주문(인덱스 1 이상)은 30% 사이즈
        if order_index >= 1:
            notional = notional * 0.3

        qty = notional / price

        # 소수점 처리 (심볼별 다를 수 있음)
        # TODO: 심볼 정보에서 정밀도 가져오기
        if 'BTC' in symbol:
            qty = round(qty, 4)
        elif 'ETH' in symbol:
            qty = round(qty, 3)
        else:
            qty = round(qty, 2)

        return qty

    def _calculate_dynamic_distance(self, symbol: str) -> float:
        """
        동적 목표 거리 계산

        Args:
            symbol: 심볼

        Returns:
            목표 거리 (bps)
        """
        strategy = self.config.strategy

        # 동적 거리 비활성화 시 고정 값 사용
        if not strategy.dynamic_distance.enabled:
            return strategy.target_distance_bps

        # 현재 스프레드와 변동성
        spread_bps = self.price_tracker.get_spread_bps(symbol)
        volatility_bps = self.price_tracker.get_volatility_bps(symbol, 10.0)

        # 동적 거리 계산
        return self.band_calculator.calculate_dynamic_distance(
            spread_bps=spread_bps,
            volatility_bps=volatility_bps,
            min_bps=strategy.dynamic_distance.min_bps,
            max_bps=strategy.dynamic_distance.max_bps,
            spread_factor=strategy.dynamic_distance.spread_factor,
            volatility_factor=strategy.dynamic_distance.volatility_factor,
        )

    async def _place_orders(self, symbol: str):
        """
        양방향 주문 배치 (2+2 전략: 다중 거리 + Lock)

        각 방향(BUY/SELL)에 num_orders_per_side개의 주문을 배치
        order_distances_bps에 지정된 거리에 각각 배치

        Args:
            symbol: 심볼
        """
        # Pre-Kill 활성 시 신규 주문 불가 (기존 주문 유지)
        if self.safety_guard.is_pre_kill_active(symbol):
            remaining = self.safety_guard.get_pre_kill_remaining(symbol)
            reason = self.safety_guard.get_pre_kill_reason(symbol)
            logger.debug(f"[{symbol}] Pre-Kill 활성 - 신규 주문 중단 ({reason}, {remaining:.1f}초 남음)")
            return

        state = self._symbol_states.get(symbol)
        if not state:
            state = SymbolState(symbol=symbol)
            self._symbol_states[symbol] = state

        # 현재 기준 가격 (mark price 우선)
        reference_price = self.price_tracker.get_reference_price(symbol)
        if reference_price <= 0:
            logger.warning(f"기준 가격 정보 없음: {symbol}")
            return

        # 2+2 전략 설정
        num_orders = self.config.strategy.num_orders_per_side
        distances = self.config.strategy.order_distances_bps
        lock_seconds = self.config.strategy.order_lock_seconds

        # 주문 리스트 초기화 (필요시)
        while len(state.buy_orders) < num_orders:
            state.buy_orders.append(None)
        while len(state.sell_orders) < num_orders:
            state.sell_orders.append(None)
        while len(state.last_target_distances_bps) < num_orders:
            state.last_target_distances_bps.append(0.0)

        placed_orders = []

        # 각 거리별로 주문 배치
        for i in range(num_orders):
            distance_bps = distances[i] if i < len(distances) else distances[-1]

            # 가격 계산
            buy_price_raw = reference_price * (1 - distance_bps / 10000)
            sell_price_raw = reference_price * (1 + distance_bps / 10000)

            # 가격 포맷팅
            if 'BTC' in symbol:
                buy_price = round(buy_price_raw, 1)
                sell_price = round(sell_price_raw, 1)
            else:
                buy_price = round(buy_price_raw, 2)
                sell_price = round(sell_price_raw, 2)

            # 수량 (인덱스에 따라 사이즈 조정: 바깥쪽=30%)
            buy_qty = self._get_order_quantity(symbol, buy_price, i)
            sell_qty = self._get_order_quantity(symbol, sell_price, i)

            # Buy 주문 i
            if not state.buy_orders[i] or not state.buy_orders[i].is_active:
                order = await self.order_manager.create_order(
                    symbol=symbol,
                    side=OrderSide.BUY,
                    price=buy_price,
                    quantity=buy_qty,
                )
                if order:
                    state.buy_orders[i] = order
                    self._stats.total_orders_placed += 1
                    self.safety_guard.set_order_lock(order.cl_ord_id, lock_seconds)
                    placed_orders.append(f"BUY{i+1}@{buy_price}({distance_bps}bps)")

            # Sell 주문 i
            if not state.sell_orders[i] or not state.sell_orders[i].is_active:
                order = await self.order_manager.create_order(
                    symbol=symbol,
                    side=OrderSide.SELL,
                    price=sell_price,
                    quantity=sell_qty,
                )
                if order:
                    state.sell_orders[i] = order
                    self._stats.total_orders_placed += 1
                    self.safety_guard.set_order_lock(order.cl_ord_id, lock_seconds)
                    placed_orders.append(f"SELL{i+1}@{sell_price}({distance_bps}bps)")

            state.last_target_distances_bps[i] = distance_bps

        state.last_reference_price = reference_price

        if placed_orders:
            logger.info(
                f"[{symbol}] 주문 배치 ({num_orders}+{num_orders}): {', '.join(placed_orders)} "
                f"[Lock: {lock_seconds}s]"
            )

    async def _place_single_order(
        self, symbol: str, side: OrderSide, order_index: int = 0
    ) -> Optional[ManagedOrder]:
        """
        단일 방향 주문 배치 (리밸런싱용, 2+2 전략 지원)

        리밸런싱 중 양방향 조건 유지를 위해 한 쪽씩 취소→배치 시 사용

        Args:
            symbol: 심볼
            side: 주문 방향 (BUY/SELL)
            order_index: 주문 인덱스 (0=가까운 주문, 1=먼 주문)

        Returns:
            생성된 주문 또는 None
        """
        # Pre-Kill 활성 시 신규 주문 불가 (기존 주문 유지)
        if self.safety_guard.is_pre_kill_active(symbol):
            remaining = self.safety_guard.get_pre_kill_remaining(symbol)
            logger.debug(f"[{symbol}] Pre-Kill 활성 - {side.value}{order_index+1} 주문 중단 ({remaining:.1f}초 남음)")
            return None

        state = self._symbol_states.get(symbol)
        if not state:
            return None

        # 현재 기준 가격 (mark price)
        reference_price = self.price_tracker.get_reference_price(symbol)
        if reference_price <= 0:
            logger.warning(f"기준 가격 정보 없음: {symbol}")
            return None

        # 2+2 전략: 인덱스에 해당하는 거리 사용
        distances = self.config.strategy.order_distances_bps
        target_distance_bps = distances[order_index] if order_index < len(distances) else distances[-1]

        # 주문 가격 계산
        if side == OrderSide.BUY:
            price_raw = reference_price * (1 - target_distance_bps / 10000)
        else:
            price_raw = reference_price * (1 + target_distance_bps / 10000)

        # 가격 포맷팅
        if 'BTC' in symbol:
            price = round(price_raw, 1)
        else:
            price = round(price_raw, 2)

        # 수량 (인덱스에 따라 사이즈 조정: 바깥쪽=30%)
        quantity = self._get_order_quantity(symbol, price, order_index)
        lock_seconds = self.config.strategy.order_lock_seconds

        order = await self.order_manager.create_order(
            symbol=symbol,
            side=side,
            price=price,
            quantity=quantity,
        )

        if order:
            self._stats.total_orders_placed += 1
            self.safety_guard.set_order_lock(order.cl_ord_id, lock_seconds)

            # 주문 리스트에 저장
            if side == OrderSide.BUY:
                while len(state.buy_orders) <= order_index:
                    state.buy_orders.append(None)
                state.buy_orders[order_index] = order
            else:
                while len(state.sell_orders) <= order_index:
                    state.sell_orders.append(None)
                state.sell_orders[order_index] = order

            # 거리 기록
            while len(state.last_target_distances_bps) <= order_index:
                state.last_target_distances_bps.append(0.0)
            state.last_target_distances_bps[order_index] = target_distance_bps

            # 기준 가격 업데이트
            state.last_reference_price = reference_price

            logger.info(
                f"[{symbol}] 단일 주문 배치: {side.value}{order_index+1} @ {price} ({target_distance_bps:.1f}bps) "
                f"[Lock: {lock_seconds}s]"
            )

        return order

    async def _check_rebalance(self, symbol: str) -> Tuple[bool, str]:
        """
        재배치 필요 여부 확인 (2+2 전략: Band 상태 기반 + 쿨다운)

        2+2 전략에서는 활성 주문이 0개인 경우에만 전체 재배치 필요
        개별 주문 Band 이탈은 부분 재배치로 처리

        Args:
            symbol: 심볼

        Returns:
            (재배치 필요 여부, 사유)
        """
        state = self._symbol_states.get(symbol)
        if not state:
            return True, "초기 배치"

        # 2+2 전략: 활성 주문 수 확인 (쿨다운보다 먼저!)
        active_buy = state.get_active_buy_count()
        active_sell = state.get_active_sell_count()
        num_orders = self.config.strategy.num_orders_per_side

        # ★ 활성 주문이 부족하면 쿨다운 무시하고 즉시 배치 (체결 대응)
        if active_buy < num_orders or active_sell < num_orders:
            if active_buy == 0:
                return True, "활성 매수 주문 없음"
            if active_sell == 0:
                return True, "활성 매도 주문 없음"
            # 부분적으로 부족한 경우
            missing = []
            if active_buy < num_orders:
                missing.append(f"BUY {active_buy}/{num_orders}")
            if active_sell < num_orders:
                missing.append(f"SELL {active_sell}/{num_orders}")
            return True, f"주문 부족: {', '.join(missing)}"

        # 쿨다운 체크 (활성 주문이 모두 있을 때만)
        now = time.time()
        if now < state.rebalance_cooldown_until:
            remaining = state.rebalance_cooldown_until - now
            logger.debug(f"[{symbol}] 쿨다운 중 ({remaining:.1f}초 남음)")
            return False, ""

        # 현재 기준 가격 (mark price 우선)
        reference_price = self.price_tracker.get_reference_price(symbol)
        if reference_price <= 0:
            return False, ""

        # 1. Band 상태 기반 부분 재배치 필요 여부 확인
        if self.config.strategy.rebalance_on_band_exit:
            # Buy 주문들 체크
            for i, order in enumerate(state.buy_orders):
                if order and order.is_active:
                    needs_rebalance, reason = self.band_calculator.needs_rebalance(
                        reference_price,
                        order.price,
                        self.config.strategy.max_distance_bps,
                    )
                    if needs_rebalance:
                        return True, f"BUY{i+1} {reason}"

            # Sell 주문들 체크
            for i, order in enumerate(state.sell_orders):
                if order and order.is_active:
                    needs_rebalance, reason = self.band_calculator.needs_rebalance(
                        reference_price,
                        order.price,
                        self.config.strategy.max_distance_bps,
                    )
                    if needs_rebalance:
                        return True, f"SELL{i+1} {reason}"

        # 2. Drift 기반 재배치 (기준가격 변동 시)
        if state.last_reference_price > 0:
            drift_bps = abs(reference_price - state.last_reference_price) / state.last_reference_price * 10000
            threshold = self.config.strategy.drift_threshold_bps
            if drift_bps > threshold:
                return True, f"Drift 초과 ({drift_bps:.1f} > {threshold} bps)"

        return False, ""

    async def _rebalance(self, symbol: str, reason: str = "", force: bool = False):
        """
        주문 재배치 (2+2 전략: 동시 처리로 업타임 최대화)

        Band 이탈한 주문만 취소/재배치하고, 다른 주문은 유지
        → 동시 처리(asyncio.gather)로 재배치 시간 최소화

        Args:
            symbol: 심볼
            reason: 재배치 사유
            force: True면 Duration/Band 조건 무시하고 모든 주문 재배치 (설정 변경 시)
        """
        state = self._symbol_states.get(symbol)
        if not state:
            return

        # Pre-Kill 활성 시 리밸런싱 스킵 (기존 주문 유지)
        if self.safety_guard.is_pre_kill_active(symbol):
            remaining = self.safety_guard.get_pre_kill_remaining(symbol)
            logger.warning(f"[{symbol}] Pre-Kill 활성 - 리밸런싱 연기 ({remaining:.1f}초 남음)")
            return

        logger.info(f"[{symbol}] 재배치 시작: {reason}")

        reference_price = self.price_tracker.get_reference_price(symbol)
        if reference_price <= 0:
            logger.warning(f"[{symbol}] 기준 가격 없음 - 재배치 스킵")
            return

        now = time.time()
        min_duration = self.config.strategy.order_lock_seconds

        # 재배치 대상 수집 (취소할 주문들)
        buy_to_rebalance = []  # [(index, order)]
        sell_to_rebalance = []

        # Buy 주문들 체크
        for i, order in enumerate(state.buy_orders):
            if order and order.is_active:
                # force 모드: Duration/Band 조건 무시하고 모든 주문 재배치
                if force:
                    buy_to_rebalance.append((i, order))
                    continue

                duration = now - order.created_at
                if duration < min_duration:
                    logger.debug(f"[{symbol}] BUY{i+1} Duration 미충족 ({duration:.1f}s) - 스킵")
                    continue

                needs_rebalance, _ = self.band_calculator.needs_rebalance(
                    reference_price, order.price, self.config.strategy.max_distance_bps
                )
                if needs_rebalance or "Drift" in reason:
                    buy_to_rebalance.append((i, order))

        # Sell 주문들 체크
        for i, order in enumerate(state.sell_orders):
            if order and order.is_active:
                # force 모드: Duration/Band 조건 무시하고 모든 주문 재배치
                if force:
                    sell_to_rebalance.append((i, order))
                    continue

                duration = now - order.created_at
                if duration < min_duration:
                    logger.debug(f"[{symbol}] SELL{i+1} Duration 미충족 ({duration:.1f}s) - 스킵")
                    continue

                needs_rebalance, _ = self.band_calculator.needs_rebalance(
                    reference_price, order.price, self.config.strategy.max_distance_bps
                )
                if needs_rebalance or "Drift" in reason:
                    sell_to_rebalance.append((i, order))

        # 교차 순차 재배치: Buy1 → Sell1 → Buy2 → Sell2 순서
        # 이렇게 하면 항상 양방향에 최소 1개씩 주문 유지
        rebalanced_orders = []

        # 재배치 순서 생성 (교차)
        max_len = max(len(buy_to_rebalance), len(sell_to_rebalance))
        rebalance_sequence = []  # [(side, index, order)]

        for idx in range(max_len):
            if idx < len(buy_to_rebalance):
                i, order = buy_to_rebalance[idx]
                rebalance_sequence.append(('BUY', i, order))
            if idx < len(sell_to_rebalance):
                i, order = sell_to_rebalance[idx]
                rebalance_sequence.append(('SELL', i, order))

        # 교차 순서로 하나씩 처리 (취소 → 즉시 재배치)
        for side, i, order in rebalance_sequence:
            # 1. 기존 주문 취소
            self.safety_guard.clear_order_lock(order.cl_ord_id)
            await self.order_manager.cancel_order(order.cl_ord_id)

            if side == 'BUY':
                state.buy_orders[i] = None
            else:
                state.sell_orders[i] = None

            # 2. 즉시 새 주문 배치
            order_side = OrderSide.BUY if side == 'BUY' else OrderSide.SELL
            new_order = await self._place_single_order(symbol, order_side, i)

            if new_order and not isinstance(new_order, Exception):
                rebalanced_orders.append(f"{side}{i+1}")

        # 기준 가격 업데이트 (Drift 재트리거 방지)
        state.last_reference_price = reference_price

        # 쿨다운 설정
        cooldown_seconds = self.config.strategy.rebalance_cooldown_seconds
        state.rebalance_cooldown_until = time.time() + cooldown_seconds
        state.last_rebalance_time = time.time()
        self._stats.total_rebalances += 1

        if rebalanced_orders:
            logger.info(f"[{symbol}] 부분 재배치 완료: {', '.join(rebalanced_orders)} [쿨다운: {cooldown_seconds}초]")
        else:
            logger.debug(f"[{symbol}] 쿨다운 설정: {cooldown_seconds}초")

    async def _calculate_effective_order_size(self):
        """
        청산 수수료 예약을 적용한 실제 주문 크기 계산

        청산용 마켓 오더 실행에 필요한 최소 금액을 예약
        """
        LIQUIDATION_FEE_RESERVE_USD = 0.50  # 청산 수수료 예약 (고정)
        MIN_ORDER_SIZE_USD = 1.0  # StandX 최소 주문 금액

        # 설정된 주문 크기 사용 (interactive.py에서 이미 검증됨)
        self._effective_order_size_usd = self.config.strategy.order_size_usd

        try:
            # ★ 동기 API를 비동기로 실행 (이벤트 루프 블로킹 방지)
            balance = await asyncio.to_thread(self.rest_client.get_balance)
            available = balance.available

            # 레버리지 및 마진 예약 설정
            leverage = self.config.strategy.leverage
            margin_reserve = self.config.strategy.margin_reserve_percent / 100

            # 청산 수수료 예약 후 사용 가능 마진
            usable_margin = available * (1 - margin_reserve) - LIQUIDATION_FEE_RESERVE_USD

            # 레버리지 적용 후 최대 노출 금액
            max_notional = usable_margin * leverage if usable_margin > 0 else 0

            # 2+2 전략: 4개 주문 (BUY 2개 + SELL 2개)
            num_symbols = len(self.config.strategy.symbols)
            num_orders = self.config.strategy.num_orders_per_side * 2  # 방향당 주문 수 × 2
            max_per_side = max_notional / (num_symbols * num_orders) if max_notional > 0 else 0

            configured_size = self.config.strategy.order_size_usd

            if configured_size > max_per_side:
                self._effective_order_size_usd = max(max_per_side, MIN_ORDER_SIZE_USD)
                logger.warning(
                    f"주문 크기 조정: 설정=${configured_size:.2f} → 실제=${self._effective_order_size_usd:.2f} "
                    f"(잔액=${available:.2f}, 레버리지={leverage}x, 최대노출=${max_notional:.2f})"
                )
            else:
                self._effective_order_size_usd = configured_size
                logger.info(
                    f"주문 크기: ${configured_size:.2f}/주문 "
                    f"(잔액=${available:.2f}, 레버리지={leverage}x, 최대노출=${max_notional:.2f})"
                )

        except Exception as e:
            logger.error(f"잔액 조회 실패, 설정값 사용: {e}")
            self._effective_order_size_usd = self.config.strategy.order_size_usd

    def _update_points_estimate(self):
        """
        포인트 추정 업데이트 (누적 방식)

        StandX 포인트 계산:
        - Band A (0-10bps): 주문금액 × 100% × (유지시간/24시간)
        - 매 체크마다 현재 활성 주문을 기반으로 포인트를 누적

        누적 방식:
        - 이전 업데이트 이후 경과 시간만큼 현재 노출 금액에 대한 포인트 적립
        - 주문이 없는 구간은 자동으로 0 포인트 (누적 안됨)
        """
        now = time.time()
        elapsed_seconds = now - self._stats.last_points_update

        # 너무 짧은 간격은 무시 (0.1초 미만)
        if elapsed_seconds < 0.1:
            return

        # 현재 활성 주문의 총 노출 금액
        total_notional = 0
        for state in self._symbol_states.values():
            total_notional += state.get_total_notional()

        # 활성 주문이 있을 때만 포인트 누적
        if total_notional > 0:
            # Band A 기준: $1 노출 = 1 point/day
            # 경과 시간(초)에 대한 포인트: notional × (elapsed / 86400)
            points_earned = total_notional * (elapsed_seconds / 86400)
            self._stats.estimated_points += points_earned
            self._stats.total_uptime_seconds += elapsed_seconds

        # 마지막 업데이트 시각 갱신
        self._stats.last_points_update = now

    # ========== Public Methods ==========

    async def start(self):
        """전략 시작"""
        symbols = self.config.strategy.symbols
        logger.info(f"메이커 파밍 시작: {symbols}")

        self._running = True
        self._stats = FarmingStats()

        # 컴포넌트 시작
        await self.price_tracker.start(symbols)

        # StandX WebSocket 구독
        await self.ws_client.start(symbols)

        # Binance WebSocket 시작 (Fill Protection용)
        if self.config.fill_protection.binance.enabled:
            try:
                await self.binance_ws.start(symbols)
                logger.info(f"Binance WebSocket 시작 완료 (trigger: {self.config.fill_protection.binance.trigger_bps}bps)")
            except Exception as e:
                logger.error(f"Binance WebSocket 시작 실패: {e}")

        # 잔액 확인 및 마진 예약 적용
        await self._calculate_effective_order_size()

        # 초기 가격 로드 대기
        await asyncio.sleep(2)

        # 초기 주문 배치 (주문 활성화 상태일 때만)
        print(f"[전략시작] _orders_enabled={self._orders_enabled}", flush=True)
        if self._orders_enabled:
            print("[전략시작] 주문 활성화 상태 - 초기 주문 배치", flush=True)
            for symbol in symbols:
                await self._place_orders(symbol)
        else:
            print("★ 주문 대기 모드 - 텔레그램에서 '주문 시작' 버튼을 눌러주세요", flush=True)
            logger.info("★ 주문 대기 모드 - 텔레그램에서 '주문 시작' 버튼을 눌러주세요")

    async def run(self):
        """메인 루프"""
        print("[RUN] ★★★ 메인 루프 시작", flush=True)
        symbols = self.config.strategy.symbols
        check_interval = self.config.strategy.check_interval_seconds
        print(f"[RUN] symbols={symbols}, check_interval={check_interval}", flush=True)
        print(f"[RUN] _orders_enabled={self._orders_enabled}", flush=True)

        # 안전 감시 태스크 시작
        safety_task = asyncio.create_task(self.safety_guard.run(symbols))

        # WebSocket 수신 태스크
        ws_task = asyncio.create_task(self.ws_client.run())

        # Binance WebSocket 수신 태스크 (Fill Protection용)
        binance_ws_task = None
        if self.config.fill_protection.binance.enabled:
            binance_ws_task = asyncio.create_task(self.binance_ws.run())

        # Fill Protection 태스크
        fill_protection_task = None
        if self.config.fill_protection.binance.enabled or self.config.fill_protection.queue.enabled:
            fill_protection_task = asyncio.create_task(self.fill_protection.run(symbols))

        # ★ 포지션 확인을 별도 태스크로 분리 (메인 루프 블로킹 방지)
        position_check_task = asyncio.create_task(self._position_check_loop())

        try:
            loop_count = 0
            print("[MAIN_LOOP] ★ while 루프 진입 직전", flush=True)
            while self._running:
                loop_count += 1
                # 매 루프마다 로그 (디버깅용)
                print(f"[LOOP#{loop_count}] running={self._running}, orders_enabled={self._orders_enabled}, force_rebalance={self._force_rebalance_requested}", flush=True)

                # 비상 정지 체크
                if self.safety_guard.is_emergency_stopped():
                    logger.error("비상 정지 상태")
                    break

                # 대기 중인 청산 처리 (포지션 확인은 별도 태스크에서)
                if self._pending_liquidations:
                    try:
                        await asyncio.wait_for(self._process_pending_liquidations(), timeout=5.0)
                    except asyncio.TimeoutError:
                        print(f"[LOOP#{loop_count}] ⚠️ 청산 처리 타임아웃 (5초)", flush=True)
                    except Exception as e:
                        print(f"[LOOP#{loop_count}] ⚠️ 청산 처리 오류: {e}", flush=True)

                # 포지션 홀딩 중에는 메이커 주문 스킵
                if self._held_position:
                    # 홀딩 상태 - 모니터링 태스크에서 관리
                    await asyncio.sleep(check_interval)
                    continue

                # 연속 체결 일시 정지 중에는 신규 주문 스킵
                if self.is_consecutive_fill_paused():
                    remaining = self.get_consecutive_fill_pause_remaining()
                    # 10초마다 로깅
                    if int(remaining) % 10 == 0 and int(remaining) > 0:
                        level = self._consecutive_fill_escalation_level
                        logger.warning(f"[연속체결보호] {level}단계 일시 정지 중... {remaining:.0f}초 남음")

                    # ★ 연속 체결 정지 시 모든 포지션 청산 (한 번만 실행)
                    if self._request_close_all_positions:
                        self._request_close_all_positions = False
                        logger.warning("★ 연속 체결 정지 - 모든 포지션 청산 시작")
                        # 모든 주문 취소
                        for symbol in symbols:
                            await self.order_manager.cancel_all_orders(symbol)
                        # 현재 포지션 조회 및 청산
                        try:
                            # ★ 동기 API를 비동기로 실행 (이벤트 루프 블로킹 방지)
                            positions = await asyncio.to_thread(self.rest_client.get_positions)
                            for pos in positions:
                                if abs(pos.size) > 0:
                                    close_side = OrderSide.SELL if pos.size > 0 else OrderSide.BUY
                                    self._pending_liquidations.append((pos.symbol, close_side, abs(pos.size)))
                                    logger.warning(f"[연속체결정지] 포지션 청산 대기열 추가: {pos.symbol} {close_side.value} {abs(pos.size)}")
                        except Exception as e:
                            logger.error(f"포지션 조회 실패: {e}")

                    # 연속 체결 정지 중에는 대기 후 다음 루프
                    await asyncio.sleep(check_interval)
                    continue

                # 정상 운영 상태
                # 단계 리셋 체크 (30분간 체결 없으면 1단계로)
                self._check_escalation_reset()

                # ★ 주문 비활성화 상태면 모든 주문 취소 후 대기
                if not self._orders_enabled:
                    # 매 루프마다 대기 상태 로그 (디버깅용)
                    print(f"[LOOP#{loop_count}] 주문 비활성화 - sleep 전 (interval={check_interval})", flush=True)

                    # 대기 상태에서는 주문 없이 계속 모니터링만
                    await asyncio.sleep(check_interval)
                    print(f"[LOOP#{loop_count}] 주문 비활성화 - sleep 후", flush=True)
                    continue

                # ★ 여기 도달하면 _orders_enabled=True
                print(f"[LOOP#{loop_count}] ★ 주문 활성화 상태 진입! force_rebalance={self._force_rebalance_requested}", flush=True)

                # ★ 강제 재배치 요청 처리 (텔레그램에서 설정 변경 시)
                if self._force_rebalance_requested:
                    self._force_rebalance_requested = False
                    print("[강제재배치] ★★★ 모든 심볼 주문 재배치 시작", flush=True)
                    logger.info("[강제재배치] ★★★ 모든 심볼 주문 재배치 시작")
                    # 주문 크기 재계산
                    await self._calculate_effective_order_size()
                    for symbol in symbols:
                        # 즉시 취소에서 상태가 초기화됨 → 항상 신규 배치
                        logger.info(f"[{symbol}] 강제 재배치 - 신규 주문 배치")
                        await self._place_orders(symbol)
                    logger.info("[강제재배치] ★★★ 모든 심볼 주문 재배치 완료")
                    continue

                for symbol in symbols:
                    # 재배치 필요 여부 확인 (Band 상태 기반)
                    needs_rebalance, reason = await self._check_rebalance(symbol)
                    if needs_rebalance:
                        print(f"[LOOP] ★ 재배치 필요: {symbol} - {reason}", flush=True)
                        # "주문 부족"인 경우 _place_orders()로 부족분 보충
                        # Band 이탈/Drift인 경우 _rebalance()로 기존 주문 재배치
                        if "주문 부족" in reason or "주문 없음" in reason:
                            print(f"[LOOP] 주문 보충 시작: {symbol}", flush=True)
                            await self._place_orders(symbol)
                        else:
                            print(f"[LOOP] 재배치 시작: {symbol}", flush=True)
                            await self._rebalance(symbol, reason)

                # 통계 업데이트
                self._update_points_estimate()

                # 주문 동기화 (5초마다만 - 너무 자주 하면 404 오류 발생)
                now = time.time()
                for symbol in symbols:
                    state = self._symbol_states.get(symbol)
                    if state:
                        if now - state.last_sync_time >= 2.0:  # 5초→2초 (업타임 개선)
                            await self.order_manager.sync_orders(symbol)
                            state.last_sync_time = now

                await asyncio.sleep(check_interval)

        except asyncio.CancelledError:
            logger.info("전략 취소됨")

        finally:
            # 태스크 정리
            safety_task.cancel()
            ws_task.cancel()

            if binance_ws_task:
                binance_ws_task.cancel()
            if fill_protection_task:
                fill_protection_task.cancel()

            try:
                await safety_task
            except asyncio.CancelledError:
                pass

            try:
                await ws_task
            except asyncio.CancelledError:
                pass

            if binance_ws_task:
                try:
                    await binance_ws_task
                except asyncio.CancelledError:
                    pass

            if fill_protection_task:
                try:
                    await fill_protection_task
                except asyncio.CancelledError:
                    pass

    async def stop(self):
        """전략 중지"""
        logger.info("메이커 파밍 중지")
        self._running = False

        # 포지션 모니터링 태스크 정리
        if self._position_monitor_task and not self._position_monitor_task.done():
            self._position_monitor_task.cancel()
            try:
                await self._position_monitor_task
            except asyncio.CancelledError:
                pass

        # Fill Protection 통계 출력
        fp_stats = self.fill_protection.get_stats()
        logger.info(
            f"Fill Protection 통계: Binance 트리거={fp_stats['binance_triggers']}, "
            f"큐 트리거={fp_stats['queue_triggers']}, 취소={fp_stats['orders_cancelled']}"
        )

        # 포지션 홀딩 통계 출력
        logger.info(
            f"포지션 홀딩 통계: 체결={self._stats.total_fills}, "
            f"익절={self._stats.total_take_profits}, 손절={self._stats.total_stop_losses}, "
            f"타임아웃={self._stats.total_timeouts}"
        )

        # 모든 주문 취소
        await self.order_manager.cancel_all_orders()

        # 컴포넌트 정리
        await self.price_tracker.stop()
        await self.ws_client.stop()
        await self.safety_guard.stop()
        await self.fill_protection.stop()
        await self.binance_ws.stop()

    def get_stats(self) -> FarmingStats:
        """통계 가져오기"""
        self._update_points_estimate()
        return self._stats

    def get_status(self) -> dict:
        """현재 상태 (2+2 전략: 다중 주문 표시)"""
        runtime = time.time() - self._stats.start_time

        # 포지션 홀딩 상태
        held_pos_info = None
        if self._held_position:
            pos = self._held_position
            price_info = self.price_tracker.get_price(pos.symbol)
            current_price = price_info.mark_price if price_info else pos.entry_price
            if pos.side == OrderSide.BUY:
                pnl_pct = ((current_price - pos.entry_price) / pos.entry_price) * 100
            else:
                pnl_pct = ((pos.entry_price - current_price) / pos.entry_price) * 100
            elapsed = time.time() - pos.entry_time

            held_pos_info = {
                'symbol': pos.symbol,
                'side': pos.side.value,
                'quantity': pos.quantity,
                'entry_price': pos.entry_price,
                'current_price': current_price,
                'pnl_pct': pnl_pct,
                'elapsed_seconds': elapsed,
                'timeout_seconds': pos.timeout_seconds,
            }

        status = {
            'running': self._running,
            'emergency_stopped': self.safety_guard.is_emergency_stopped(),
            'holding_position': self._held_position is not None,
            'held_position': held_pos_info,
            'consecutive_fill_paused': self.is_consecutive_fill_paused(),
            'consecutive_fill_pause_remaining': self.get_consecutive_fill_pause_remaining(),
            'consecutive_fill_escalation_level': self._consecutive_fill_escalation_level,
            'runtime_seconds': runtime,
            'runtime_hours': runtime / 3600,
            'symbols': {},
            'stats': {
                'orders_placed': self._stats.total_orders_placed,
                'orders_cancelled': self._stats.total_orders_cancelled,
                'rebalances': self._stats.total_rebalances,
                'fills': self._stats.total_fills,
                'liquidations': self._stats.total_liquidations,
                'take_profits': self._stats.total_take_profits,
                'stop_losses': self._stats.total_stop_losses,
                'timeouts': self._stats.total_timeouts,
                'estimated_points': self._stats.estimated_points,
                'consecutive_fill_pauses': self._stats.consecutive_fill_pauses,
                'uptime_seconds': self._stats.total_uptime_seconds,
                'uptime_percent': (self._stats.total_uptime_seconds / max(1, runtime)) * 100,
            },
            'strategy': {
                'type': f"{self.config.strategy.num_orders_per_side}+{self.config.strategy.num_orders_per_side}",
                'distances_bps': self.config.strategy.order_distances_bps,
            },
        }

        for symbol, state in self._symbol_states.items():
            price = self.price_tracker.get_price(symbol)

            symbol_status = {
                'mid_price': price.mid_price if price else 0,
                'mark_price': price.mark_price if price else 0,
                'reference_price': price.reference_price if price else 0,
                'spread_bps': price.spread_bps if price else 0,
                'volatility_bps': self.price_tracker.get_volatility_bps(symbol, 10.0),
                'last_target_distances_bps': state.last_target_distances_bps,
                'cooldown_remaining': max(0, state.rebalance_cooldown_until - time.time()),
                'active_buy_count': state.get_active_buy_count(),
                'active_sell_count': state.get_active_sell_count(),
                'total_notional': state.get_total_notional(),
                'buy_orders': [],
                'sell_orders': [],
            }

            # Buy 주문들
            for i, order in enumerate(state.buy_orders):
                if order:
                    symbol_status['buy_orders'].append({
                        'index': i + 1,
                        'price': order.price,
                        'quantity': order.quantity,
                        'status': order.status.value,
                        'notional_usd': order.notional_usd,
                    })
                else:
                    symbol_status['buy_orders'].append(None)

            # Sell 주문들
            for i, order in enumerate(state.sell_orders):
                if order:
                    symbol_status['sell_orders'].append({
                        'index': i + 1,
                        'price': order.price,
                        'quantity': order.quantity,
                        'status': order.status.value,
                        'notional_usd': order.notional_usd,
                    })
                else:
                    symbol_status['sell_orders'].append(None)

            status['symbols'][symbol] = symbol_status

        return status
