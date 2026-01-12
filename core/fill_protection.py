"""
체결 방지 보호 장치 (Fill Protection)

두 가지 핵심 기능:
1. Binance Mark Price 선행 감지 - StandX보다 100-500ms 빠름
2. 오더북 큐 포지션 모니터링 - 앞 물량 급감 시 선제 취소
"""
import asyncio
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Deque, Dict, List, Optional

from ..api.binance_ws_client import BinanceWebSocket, BinanceMarkPrice
from ..api.websocket_client import StandXWebSocket, OrderbookData
from .order_manager import OrderManager, ManagedOrder
from .safety_guard import SafetyGuard
from ..api.rest_client import OrderSide
from ..utils.logger import get_logger

logger = get_logger('fill_protection')


class ProtectionAction(Enum):
    """보호 조치"""
    NONE = "none"
    CANCEL_BUY = "cancel_buy"       # 매수 주문만 취소
    CANCEL_SELL = "cancel_sell"     # 매도 주문만 취소
    CANCEL_ALL = "cancel_all"       # 모든 주문 취소


@dataclass
class BinanceProtectionConfig:
    """Binance 선행 감지 설정"""
    enabled: bool = True
    # 변동 트리거 (bps) - 이 이상 변동 시 보호 발동
    trigger_bps: float = 3.0
    # 감지 윈도우 (초)
    window_seconds: float = 0.5
    # 연속 트리거 방지 쿨다운 (초)
    cooldown_seconds: float = 0.5


@dataclass
class QueueProtectionConfig:
    """오더북 큐 프로텍션 설정"""
    enabled: bool = True
    # 앞 물량 감소 임계값 (%) - 이 비율 이상 감소 시 취소
    drop_threshold_percent: float = 30.0
    # 모니터링 윈도우 (초)
    window_seconds: float = 2.0
    # 최소 앞 물량 (USD) - 이 이하면 즉시 취소
    min_queue_ahead_usd: float = 100.0


@dataclass
class FillProtectionConfig:
    """체결 방지 보호 설정"""
    binance: BinanceProtectionConfig = field(default_factory=BinanceProtectionConfig)
    queue: QueueProtectionConfig = field(default_factory=QueueProtectionConfig)
    # 체크 주기 (초)
    check_interval_seconds: float = 0.1
    # 스마트 보호: Lock 경과 임계값 (초)
    # 이 시간 경과 전에는 Fill Protection 비활성화 (포인트 적립 보장)
    smart_protection_threshold_seconds: float = 2.5


@dataclass
class ProtectionEvent:
    """보호 이벤트"""
    action: ProtectionAction
    reason: str
    symbol: str
    details: dict
    timestamp: float


# 콜백 타입
ProtectionCallback = Callable[[ProtectionEvent], None]


@dataclass
class QueueSnapshot:
    """오더북 큐 스냅샷"""
    timestamp: float
    symbol: str
    # 각 가격 레벨별 물량 (price -> size in base currency)
    bid_levels: Dict[float, float]
    ask_levels: Dict[float, float]


class FillProtection:
    """
    체결 방지 보호 장치

    핵심 전략:
    1. Binance mark price가 급등 → StandX 매수 주문 취소 (매수가에 도달할 위험)
    2. Binance mark price가 급락 → StandX 매도 주문 취소 (매도가에 도달할 위험)
    3. 내 주문 가격의 앞 물량이 급감 → 해당 주문 취소 (큐 소진 → 체결 위험)
    """

    def __init__(
        self,
        binance_ws: BinanceWebSocket,
        standx_ws: StandXWebSocket,
        order_manager: OrderManager,
        safety_guard: SafetyGuard,
        config: Optional[FillProtectionConfig] = None,
    ):
        """
        Args:
            binance_ws: Binance WebSocket 클라이언트
            standx_ws: StandX WebSocket 클라이언트
            order_manager: 주문 관리자
            safety_guard: 안전 장치 (Lock 해제용)
            config: 보호 설정
        """
        self.binance_ws = binance_ws
        self.standx_ws = standx_ws
        self.order_manager = order_manager
        self.safety_guard = safety_guard
        self.config = config or FillProtectionConfig()

        self._running = False
        self._callbacks: List[ProtectionCallback] = []

        # 보호 발동 쿨다운
        self._binance_cooldown: Dict[str, float] = {}  # symbol -> cooldown_until
        self._queue_cooldown: Dict[str, float] = {}

        # 오더북 히스토리 (큐 프로텍션용)
        self._orderbook_history: Dict[str, Deque[QueueSnapshot]] = {}

        # 통계
        self._stats = {
            "binance_triggers": 0,
            "queue_triggers": 0,
            "orders_cancelled": 0,
        }

        # 오더북 콜백 등록
        self.standx_ws.on_orderbook(self._on_orderbook_update)

    def on_protection_event(self, callback: ProtectionCallback):
        """보호 이벤트 콜백 등록"""
        self._callbacks.append(callback)

    def _emit_event(self, action: ProtectionAction, reason: str, symbol: str, details: dict):
        """이벤트 발생"""
        event = ProtectionEvent(
            action=action,
            reason=reason,
            symbol=symbol,
            details=details,
            timestamp=time.time(),
        )

        for callback in self._callbacks:
            try:
                callback(event)
            except Exception as e:
                logger.error(f"보호 콜백 오류: {e}")

        # 로깅
        logger.warning(f"[FillProtection] {action.value}: {symbol} - {reason}")

    def get_stats(self) -> dict:
        """통계 조회"""
        return self._stats.copy()

    # ========== Binance 선행 감지 ==========

    def _check_binance_trigger(self, standx_symbol: str) -> Optional[ProtectionAction]:
        """
        Binance 가격 기반 보호 트리거 확인

        로직:
        - Binance mark price 급등 → 매수 주문 취소 (가격이 올라가면 내 매수가에 도달)
        - Binance mark price 급락 → 매도 주문 취소 (가격이 내려가면 내 매도가에 도달)

        Args:
            standx_symbol: StandX 심볼 (예: BTC-USD)

        Returns:
            보호 조치 또는 None
        """
        if not self.config.binance.enabled:
            return None

        # 쿨다운 체크
        now = time.time()
        if standx_symbol in self._binance_cooldown:
            if now < self._binance_cooldown[standx_symbol]:
                return None

        # Binance 가격 변동 확인
        change_bps = self.binance_ws.get_price_change_bps(
            standx_symbol,
            self.config.binance.window_seconds,
        )

        trigger_bps = self.config.binance.trigger_bps

        if abs(change_bps) < trigger_bps:
            return None

        # 트리거 발동
        self._binance_cooldown[standx_symbol] = now + self.config.binance.cooldown_seconds

        if change_bps > 0:
            # 가격 상승 → 매수 주문 체결 위험 → 매수 취소
            return ProtectionAction.CANCEL_BUY
        else:
            # 가격 하락 → 매도 주문 체결 위험 → 매도 취소
            return ProtectionAction.CANCEL_SELL

    # ========== 오더북 큐 프로텍션 ==========

    def _on_orderbook_update(self, data: OrderbookData):
        """StandX 오더북 업데이트 처리"""
        symbol = data.symbol

        if symbol not in self._orderbook_history:
            self._orderbook_history[symbol] = deque(maxlen=50)

        # 오더북 스냅샷 저장
        bid_levels = {float(b[0]): float(b[1]) for b in data.bids[:20]}
        ask_levels = {float(a[0]): float(a[1]) for a in data.asks[:20]}

        self._orderbook_history[symbol].append(QueueSnapshot(
            timestamp=time.time(),
            symbol=symbol,
            bid_levels=bid_levels,
            ask_levels=ask_levels,
        ))

    def _calculate_queue_ahead(
        self,
        order: ManagedOrder,
        snapshot: QueueSnapshot,
    ) -> float:
        """
        내 주문 앞에 있는 물량 계산 (USD)

        예: 내 BUY 주문이 $50,000이면, $50,000 이상의 bid 물량 합산
        (내 가격보다 높은 bid = 나보다 먼저 체결될 주문들)

        Args:
            order: 주문
            snapshot: 오더북 스냅샷

        Returns:
            앞 물량 (USD)
        """
        if order.side == OrderSide.BUY:
            # 매수 주문: 내 가격보다 높거나 같은 bid들이 앞에 있음
            # (더 높은 가격에 매수하려는 사람들이 먼저 체결됨)
            queue = sum(
                qty * price
                for price, qty in snapshot.bid_levels.items()
                if price >= order.price
            )
        else:
            # 매도 주문: 내 가격보다 낮거나 같은 ask들이 앞에 있음
            # (더 낮은 가격에 매도하려는 사람들이 먼저 체결됨)
            queue = sum(
                qty * price
                for price, qty in snapshot.ask_levels.items()
                if price <= order.price
            )
        return queue

    def _check_queue_drop(self, symbol: str, order: ManagedOrder) -> bool:
        """
        큐 물량 급감 확인

        Args:
            symbol: 심볼
            order: 주문

        Returns:
            True면 위험 (취소 필요)
        """
        if symbol not in self._orderbook_history:
            return False

        history = list(self._orderbook_history[symbol])
        if len(history) < 2:
            return False

        now = time.time()
        window = self.config.queue.window_seconds

        # 윈도우 시작 시점과 현재 시점의 스냅샷
        old_snapshots = [s for s in history if now - s.timestamp > window * 0.5]
        new_snapshots = [s for s in history if now - s.timestamp <= window * 0.5]

        if not old_snapshots or not new_snapshots:
            return False

        old_queue = self._calculate_queue_ahead(order, old_snapshots[-1])
        new_queue = self._calculate_queue_ahead(order, new_snapshots[-1])

        # 1. 절대 물량 체크
        if new_queue < self.config.queue.min_queue_ahead_usd:
            logger.debug(f"큐 물량 부족: {order.cl_ord_id} ({new_queue:.0f} USD)")
            return True

        # 2. 감소율 체크
        if old_queue > 0:
            drop_percent = (1 - new_queue / old_queue) * 100
            if drop_percent > self.config.queue.drop_threshold_percent:
                logger.debug(
                    f"큐 물량 급감: {order.cl_ord_id} "
                    f"({old_queue:.0f} -> {new_queue:.0f} USD, -{drop_percent:.0f}%)"
                )
                return True

        return False

    def _check_queue_protection(self, symbol: str) -> List[ManagedOrder]:
        """
        큐 프로텍션 확인

        Args:
            symbol: 심볼

        Returns:
            취소해야 할 주문 목록
        """
        if not self.config.queue.enabled:
            return []

        # 쿨다운 체크
        now = time.time()
        if symbol in self._queue_cooldown:
            if now < self._queue_cooldown[symbol]:
                return []

        orders_to_cancel = []

        for order in self.order_manager.get_active_orders(symbol):
            if self._check_queue_drop(symbol, order):
                orders_to_cancel.append(order)

        if orders_to_cancel:
            self._queue_cooldown[symbol] = now + 1.0  # 1초 쿨다운

        return orders_to_cancel

    # ========== 보호 실행 ==========

    async def _execute_protection(
        self,
        symbol: str,
        action: ProtectionAction,
        reason: str,
        specific_orders: Optional[List[ManagedOrder]] = None,
    ) -> int:
        """
        보호 조치 실행 (스마트 보호 적용)

        스마트 보호 로직:
        - Lock 경과 < threshold: Fill Protection 비활성화 (포인트 적립 보장)
        - Lock 경과 >= threshold: Fill Protection 활성화 (체결 방지)
        - Hard Kill (30bps)은 SafetyGuard에서 별도 처리

        Args:
            symbol: 심볼
            action: 보호 조치
            reason: 사유
            specific_orders: 특정 주문만 취소 (None이면 action에 따라)

        Returns:
            취소된 주문 수
        """
        cancelled = 0
        skipped = 0
        threshold = self.config.smart_protection_threshold_seconds

        # 대상 주문 목록 결정
        if specific_orders:
            orders_to_check = specific_orders
        else:
            orders_to_check = self.order_manager.get_active_orders(symbol)

        for order in orders_to_check:
            # action에 따른 취소 대상 여부 확인
            should_cancel = False

            if specific_orders:
                should_cancel = True  # specific_orders는 이미 필터링됨
            elif action == ProtectionAction.CANCEL_ALL:
                should_cancel = True
            elif action == ProtectionAction.CANCEL_BUY and order.side == OrderSide.BUY:
                should_cancel = True
            elif action == ProtectionAction.CANCEL_SELL and order.side == OrderSide.SELL:
                should_cancel = True

            if not should_cancel:
                continue

            # ★ 스마트 보호: Lock 경과 시간 확인
            elapsed = self.safety_guard.get_lock_elapsed_seconds(order.cl_ord_id)

            if elapsed is not None and elapsed < threshold:
                # Lock threshold 미만: 포인트 적립 보장 (취소 안 함)
                logger.debug(
                    f"[스마트보호] {order.cl_ord_id} 취소 스킵 "
                    f"(Lock {elapsed:.2f}s < {threshold}s, 포인트 보장)"
                )
                skipped += 1
                continue

            # Lock threshold 이상 또는 Lock 없음: Fill Protection 발동
            self.safety_guard.clear_order_lock(order.cl_ord_id)
            await self.order_manager.cancel_order(order.cl_ord_id)
            cancelled += 1

            if elapsed is not None:
                logger.info(
                    f"[스마트보호] {order.cl_ord_id} 취소 "
                    f"(Lock {elapsed:.2f}s >= {threshold}s)"
                )

        if cancelled > 0:
            # 통계 업데이트
            self._stats["orders_cancelled"] += cancelled

            # 이벤트 발생
            self._emit_event(action, reason, symbol, {"cancelled": cancelled, "skipped": skipped})

        return cancelled

    # ========== 메인 루프 ==========

    async def run(self, symbols: List[str]):
        """
        보호 감시 루프

        Args:
            symbols: 감시할 심볼 목록 (StandX 심볼)
        """
        self._running = True
        logger.info(f"Fill Protection 시작: {symbols}")
        logger.info(
            f"설정: Binance trigger={self.config.binance.trigger_bps}bps, "
            f"Queue drop={self.config.queue.drop_threshold_percent}%"
        )

        while self._running:
            try:
                for symbol in symbols:
                    # 1. Binance 선행 감지 (최우선)
                    binance_action = self._check_binance_trigger(symbol)
                    if binance_action:
                        change_bps = self.binance_ws.get_price_change_bps(
                            symbol,
                            self.config.binance.window_seconds,
                        )
                        direction = "상승" if binance_action == ProtectionAction.CANCEL_BUY else "하락"

                        cancelled = await self._execute_protection(
                            symbol,
                            binance_action,
                            f"Binance mark price 급{direction} ({change_bps:+.1f} bps)",
                        )

                        if cancelled > 0:
                            self._stats["binance_triggers"] += 1

                        continue  # 이번 루프는 여기서 종료

                    # 2. 오더북 큐 프로텍션
                    queue_orders = self._check_queue_protection(symbol)
                    if queue_orders:
                        cancelled = await self._execute_protection(
                            symbol,
                            ProtectionAction.CANCEL_ALL,
                            f"오더북 큐 물량 급감 ({len(queue_orders)}건)",
                            specific_orders=queue_orders,
                        )

                        if cancelled > 0:
                            self._stats["queue_triggers"] += 1

                await asyncio.sleep(self.config.check_interval_seconds)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Fill Protection 오류: {e}")
                await asyncio.sleep(0.5)

        logger.info("Fill Protection 중지")

    async def stop(self):
        """감시 중지"""
        self._running = False
        logger.info(f"Fill Protection 통계: {self._stats}")
